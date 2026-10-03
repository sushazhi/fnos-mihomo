#!/usr/bin/env python3
"""
build.py - mihomo fnOS 应用统一打包脚本（跨平台，取代了早期的 build-fpk.sh）

用法:
    python build.py                       # 默认 x86，包版本**跟随 mihomo 内核版本**
    python build.py --arch arm
    python build.py --version 1.0.7       # 显式指定包版本（覆盖内核跟随）
    python build.py --force               # 强制重新下载所有依赖

版本号策略:
    - 默认 **包版本 = 内核版本**：取 GitHub `/releases/latest` 的 tag，
      去掉 `v` 前缀后作为 manifest 的 version（`v1.19.32` → `1.19.32`），
      最终产物即 `mihomo-1.19.32-x86.fpk`。
    - `--version` 显式指定时以它为准。
    - 内核版本取不到（离线）或 tag 不是数字版本（如 `alpha-xxxx`）时，
      回退读 manifest 里手写的 version。见 kernel_version_to_app_version。

下载策略（构建期所有联网下载统一遵守）:
    - 顺序固定为 **先直连、失败再依次走加速源**（https://gh.dpik.top/、
      https://gh-proxy.org/，前缀拼接原始 URL）。直连能通就不绕道第三方。
      加速源**只用于 GitHub 来源**（内核 / geo 数据 / 面板资产）。
    - **fnpack 例外，走纯直连**：static2.fnnas.com 是飞牛自家静态站，
      不是 GitHub 资产，套第三方前缀代理没有收益（见 ensure_fnpack）。
    - 每个源都有**超时**（默认 60s，见 DOWNLOAD_TIMEOUT），失败立刻换源，
      不会在某个被墙地址上挂满十几分钟。
    - 可用环境变量覆盖：
          MIHOMO_DL_TIMEOUT     单源超时秒数（默认 60）
          MIHOMO_FNPACK_TIMEOUT fnpack 下载超时秒数（默认 120）
          MIHOMO_API_TIMEOUT    GitHub API 超时秒数（默认 15）
          MIHOMO_DL_MIRRORS     加速源列表，逗号分隔；留空则只用直连

设计取向（参照 fnos-qbittorrent/build.py）:
    - 按**开发机**平台自动选择官方 fnpack 二进制（Windows/Linux/macOS 都有）
    - 只用标准库：内置 tarfile / zipfile / gzip / urllib，无需外部 unzip/tar/curl
    - 复用 .local-build/ 缓存，避免重复下载
    - **打包后重建 tar 权限**（关键，见 fix_fpk_permissions）

为什么必须是「打包后修权限」而不能靠 os.chmod:
    Windows 上 fnpack 会把所有文件写成 0666（目录 0777），实测确认：
        cmd/config_callback  0o666
        cmd/main             0o666
    装机后生命周期脚本不可执行，升级回调里 `-x $MAIN_SCRIPT` 会失败。
    而 Windows 文件系统本身也存不住 Unix 执行位，所以在**打包前** chmod 无效，
    唯一可靠的做法是拿到 .fpk 之后重写 tar 内的 mode 字段。
    （在 Linux 上 fnpack 会正确保留 0755，所以这段修复在 Linux 上是幂等的。）
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(PROJECT_DIR, ".local-build")
STAGE_DIR = os.path.join(BUILD_DIR, "packroot")   # 交给 fnpack 打的目录
TMP_DIR = os.path.join(BUILD_DIR, "tmp")          # 构建期临时文件（在 packroot 之外）
VERSION_FILE = os.path.join(BUILD_DIR, "versions.json")
MANIFEST_FILE = os.path.join(PROJECT_DIR, "manifest")

FNPACK_BASE = "https://static2.fnnas.com/fnpack/fnpack-1.2.3"
FNPACK_VER = "1.2.3"

# ── 下载策略 ─────────────────────────────────────────────────
# 顺序固定为「先直连，再加速源」：直连能通时就不必绕道第三方 ——
# 既不额外消耗加速源的免费流量，也不把下载内容交给中间人；
# 只有直连失败（超时 / 拒连 / 内容为空）才逐级回退到加速源。
#
# 加速源用法：**前缀拼接**，即 `<加速源前缀><原始 github.com URL>`。
# 两个源都已实测可用（HTTP 200，返回真实 GitHub 内容）：
#     https://gh.dpik.top/https://raw.githubusercontent.com/...
#     https://gh-proxy.org/https://raw.githubusercontent.com/...
#
# 适用范围：**只用于 GitHub 来源**（内核 / geo 数据 / 面板资产）。
# fnpack 走 static2.fnnas.com（飞牛自家静态站），不套加速源，见 ensure_fnpack。
#
# ⚠️ 为什么必须补上「超时」：`urlopen(timeout=...)` 是**单次 socket 读超时**，
#    不是整次下载的硬上限；而原先默认 300s 意味着一个被墙/黑洞的地址
#    要挂满 300s 才轮到下一个源。直连被墙 + 多个加速源依次试探，
#    最坏要等十几分钟。现在每个源只给 DOWNLOAD_TIMEOUT 秒，
#    失败立刻换下一个源，整体耗时可控且可预期。
DOWNLOAD_TIMEOUT = int(os.environ.get("MIHOMO_DL_TIMEOUT", "60"))
FNPACK_TIMEOUT = int(os.environ.get("MIHOMO_FNPACK_TIMEOUT", "120"))
API_TIMEOUT = int(os.environ.get("MIHOMO_API_TIMEOUT", "15"))


def _parse_mirrors(raw):
    """解析 MIHOMO_DL_MIRRORS（逗号分隔）；空串代表**关闭加速源**。"""
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if item:
            out.append(item.rstrip("/") + "/")
    return out


# 加速源列表，按顺序回退。MIHOMO_DL_MIRRORS 可覆盖：
#     MIHOMO_DL_MIRRORS=""                              → 只用直连
#     MIHOMO_DL_MIRRORS="https://gh.dpik.top/"          → 只用这一个加速源
_DEFAULT_MIRRORS = [
    "https://gh.dpik.top/",
    "https://gh-proxy.org/",
]
_mirrors_env = os.environ.get("MIHOMO_DL_MIRRORS")
MIRRORS = list(_DEFAULT_MIRRORS) if _mirrors_env is None else _parse_mirrors(_mirrors_env)

MIHOMO_API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
RULES_API = "https://api.github.com/repos/MetaCubeX/meta-rules-dat/releases/latest"

# 目标架构映射。
# 与 fnos-qbittorrent 不同：mihomo 的 fpk **分架构出包**，因为内核是各架构
# 独立的 ELF，必须与 manifest 的 platform 严格对应。
ARCH_MAP = {
    "x86": {"mihomo": "amd64", "manifest": "x86", "elf": "x86-64"},
    "arm": {"mihomo": "arm64", "manifest": "arm", "elf": "aarch64"},
}


def _force_utf8_stdout():
    """让 stdout/stderr 用 UTF-8，避免 Windows 控制台默认 GBK 编码时崩溃。

    ⚠️ 真实踩过的坑：Windows 上 `sys.stdout` 默认编码是 GBK，
       日志里的 `✓`（U+2713）等字符会直接抛
       `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'`，
       导致构建在「下载成功、准备打印结果」这一步莫名中断。
       这里统一改 UTF-8；失败也不致命（退化为 errors="replace"）。
    """
    for stream in ("stdout", "stderr"):
        s = getattr(sys, stream, None)
        if s is None:
            continue
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 老 Python / 被重定向的流可能没有 reconfigure，忽略即可
            pass


def log(msg):
    try:
        sys.stdout.write(str(msg) + "\n")
    except UnicodeEncodeError:
        # 兜底：极端环境下重配也无效时，退化成可编码的替代写法，绝不中断构建
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(str(msg).encode(enc, "replace").decode(enc, "replace") + "\n")
    sys.stdout.flush()


def warn(msg):
    log("  [WARN] " + str(msg))


def die(msg):
    log("ERROR: " + str(msg))
    sys.exit(1)


# ── 平台 / fnpack ────────────────────────────────────────────

def get_platform():
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s.startswith("darwin"):
        return "darwin"
    return "linux"


def get_platform_arch():
    m = platform.machine().lower()
    if m in ("aarch64", "arm64", "armv8l", "arm"):
        return "arm64"
    return "amd64"


def get_fnpack_url():
    """按**开发机**平台返回官方 fnpack 下载地址。

    ⚠️ 官方文档把 Linux ARM 写成 `-linux-arm64`，但**实测该 URL 404**，
       真正可用的是 `-linux-arm`（命名在版本间反转过）。已实测验证：
           fnpack-1.2.3-windows-amd64  200
           fnpack-1.2.3-linux-amd64    200
           fnpack-1.2.3-linux-arm      200
           fnpack-1.2.3-linux-arm64    404
    """
    plat = get_platform()
    if plat == "windows":
        fnpack_arch = "amd64"
    elif plat == "darwin":
        fnpack_arch = get_platform_arch()
    else:  # linux
        fnpack_arch = "arm" if get_platform_arch() == "arm64" else "amd64"
    return "%s-%s-%s" % (FNPACK_BASE, plat, fnpack_arch)


# ── 版本缓存 ─────────────────────────────────────────────────

def load_versions():
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_version(component, version):
    os.makedirs(BUILD_DIR, exist_ok=True)
    v = load_versions()
    v[component] = version
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False, indent=2)


def version_match(component, expected):
    return load_versions().get(component) == expected


# ── 网络 ─────────────────────────────────────────────────────

def http_get(url, timeout=None):
    if timeout is None:
        timeout = DOWNLOAD_TIMEOUT
    req = urllib.request.Request(url, headers={"User-Agent": "mihomo-fnos-build/1.0"})
    return urllib.request.urlopen(req, timeout=timeout)


def read_url(url, timeout=None):
    with http_get(url, timeout) as resp:
        return resp.read()


def ordered_sources(url, mirrors=None):
    """返回某个 URL 的候选下载地址：**先直连，后加速源**。

    直连失败是按「整段 URL」判定的（含 302 跳转到 objects.githubusercontent.com
    之后的结果），因此这里把完整原始 URL 交给加速源拼接，
    形如 `https://gh.dpik.top/https://github.com/.../mihomo-linux-amd64-v1.2.3.gz`。

    `mirrors` 传空列表即「只用直连」（对齐 MIHOMO_DL_MIRRORS=""）。
    """
    srcs = [url]
    for base in (MIRRORS if mirrors is None else mirrors):
        srcs.append(base + url)
    return srcs


def json_api(url, timeout=None):
    """取 GitHub API 的 JSON；失败返回 None（不致命，调用方决定回退策略）。"""
    try:
        with http_get(url, timeout or API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        warn("网络获取失败（%s）：%s" % (url, e))
        return None


def download(url, out_file, description, component=None, version=None, force=False,
             mirrors=None, timeout=None):
    """带缓存的下载：**先直连，失败再走加速源**；写入**临时文件**再原子替换。

    ⚠️ 必须写临时文件：直接 `open(out_file,'wb')` 会先截断目标，下载失败时
       连上一次的正确文件一起毁掉（原 bash 脚本的血泪教训）。
    ⚠️ 临时文件放在目标**同目录**：Windows 上 os.replace 跨盘符会失败
       （临时目录在 C:，项目在 D:）。
    ⚠️ `mirrors` 的 None / [] 语义**不同**，别当成可以省略：
           mirrors=None（默认）→ 用全局 MIRRORS，即「直连 + 加速源」
           mirrors=[]          → **只用直连**（fnpack 就是靠这个显式关掉加速源）
       二者一旦混用，就会把本该直连的下载也套上第三方代理。
    """
    if not force and component and version and os.path.exists(out_file) \
            and os.path.getsize(out_file) > 0 and version_match(component, version):
        log("  使用缓存 %s (%s)" % (description, version))
        return True

    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    tmp = out_file + ".part"

    last_err = ""
    for u in ordered_sources(url, mirrors):
        try:
            log("    尝试 %s" % u[:100])
            data = read_url(u, timeout)
            if not data:
                last_err = "下载内容为空"
                continue
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out_file)   # 同目录，原子
            if component and version:
                save_version(component, version)
            log("  ✓ %s (%d 字节)" % (description, len(data)))
            return True
        except Exception as e:
            last_err = str(e)
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    log("  ✗ %s 下载失败：%s" % (description, last_err))
    return False


# ── 通用工具 ─────────────────────────────────────────────────

def read_url_to_file(url, out_file, attempts=4, timeout=None):
    """带断点续传的下载，专治大文件被中途掐断。

    实测症状：60MB 的内核 / 20MB 的 arm 内核经常下到一半就
    `IncompleteRead(7146540 bytes read, 13663052 more expected)`。
    这类截断文件如果直接当成果用，就会打出「内核损坏」的包（装机才炸）。
    做法：已下的部分保留，用 HTTP Range 续传，直到拿满 Content-Length；
    每次失败退避重试。

    ⚠️ 续传只对**可选源**有意义，所以每个候选源单独调用本函数：
       一个源续传失败后换下一个源，必须**先把半截文件删掉**，
       否则下一个源会把别人的半截文件当成本源的已下部分续传 —— 得到的是
       两个源字节拼接的垃圾（能过大小的校验，但解压/运行必炸）。
    """
    import time as _t
    if timeout is None:
        timeout = DOWNLOAD_TIMEOUT
    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    last = ""
    for i in range(attempts):
        have = os.path.getsize(out_file) if os.path.exists(out_file) else 0
        req = urllib.request.Request(url, headers={
            "User-Agent": "mihomo-fnos-build/1.0"})
        if have:
            req.add_header("Range", "bytes=%d-" % have)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                total = resp.headers.get("Content-Length")
                try:
                    total = int(total) if total is not None else None
                except (TypeError, ValueError):
                    total = None
                if total is not None:
                    total += (have if resp.status == 206 else 0)
                mode = "ab" if (have and resp.status == 206) else "wb"
                if mode == "wb":
                    have = 0
                with open(out_file, mode) as f:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
            got = os.path.getsize(out_file)
            if total is None or got >= total:
                return True
            last = "仅收到 %d/%d 字节" % (got, total)
        except Exception as e:
            last = str(e)
        _t.sleep(2 + i * 2)
    return False


def copy_tree(src, dst):
    """复制目录/文件到暂存区，顺带跳过不该进包的东西。

    ⚠️ 必须排除 `__pycache__` / `*.pyc`：`app/ui_server.py` 一旦被 import 过
    （测试、或本地手跑）就会在旁边留下 `__pycache__`，它会被打进 app.tgz。
    这些文件是宿主机字节码，既无用又让产物随构建机变化。
    """
    if not os.path.exists(src):
        return
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                      "*.pyo", ".DS_Store"))
    else:
        if src.endswith((".pyc", ".pyo")) or "__pycache__" in src:
            return
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.copy2(src, dst)
    if not os.path.exists(src):
        return
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.copy2(src, dst)


def read_manifest_version():
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip()
    return ""


def kernel_version_to_app_version(tag):
    """把内核 tag 规范化成 fpk 包版本号。

    内核 tag 形如 `v1.19.32`，而飞牛 manifest 的 `version` 示例一律是
    **裸数字点分**（`1.0.0`、`2.1.3-beta`），`v1.19.32` 那种带 v 前缀的写法
    只出现在 Clash API 的返回里，**从未作为 manifest 取值出现过**。
    fnpack 是否接受 `v` 前缀无法在本机复现验证，所以这里采取保守做法：
    去掉 `v` 前缀，并校验结果确实是「数字开头」，不合法就直接放手
    （返回 ""，由调用方回退到 manifest 里手写的版本号）。

    例：`v1.19.32` → `1.19.32`；`1.19.32` → `1.19.32`；`alpha-2024` → ""。
    """
    if not tag:
        return ""
    v = tag.strip()
    if v[:1] in ("v", "V"):
        v = v[1:].strip()
    # 必须是「数字.数字...」这类点分数字版本（允许 -beta / +meta 后缀）
    if not re.match(r"^\d+(\.\d+)*([-+.][0-9A-Za-z.\-]+)?$", v):
        return ""
    return v


# ── ELF / geo 校验 ───────────────────────────────────────────

def elf_arch(path):
    """读 ELF 头判断架构（不依赖外部 `file`，跨平台）。

    原 bash 脚本用 `file -b | grep x86-64|aarch64`；Windows 原生 Python 环境
    未必有 file，直接解析 ELF 头更可靠，也顺便做了魔数校验。
    """
    with open(path, "rb") as f:
        hdr = f.read(20)
    if len(hdr) < 20:
        return None
    if hdr[0:4] != b"\x7fELF":
        return None
    machine = hdr[18] | (hdr[19] << 8)
    if machine == 0x3E:
        return "x86-64"
    if machine == 0xB7:
        return "aarch64"
    return "unknown(0x%x)" % machine


def geodata_valid(path, kind):
    """按**内容**判定完整性，而不是只看大小。

    2026-10-03 启动超时事故的第二道防线：
      * geoip.metadb 是 MMDB，元数据标记 "MaxMind.com" 固定在文件尾部，
        截断文件必然缺失 → 可靠的「下全了没有」判据（只扫尾部 64KB 即可）；
      * geosite.dat 是 protobuf，首字节应为 field 1 的 tag 0x0a；
        只查大小会把「截断到 1MB 以上」的坏文件判成好的。
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    size = os.path.getsize(path)
    if kind == "geoip":
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            return b"MaxMind.com" in f.read()
    if kind == "geosite":
        if size <= 1000000:
            return False
        with open(path, "rb") as f:
            return f.read(1) == b"\x0a"
    return False


# ── [1] 内核 ─────────────────────────────────────────────────

def fetch_kernel(arch_key, force):
    """下载内核，并把内核版本号回传给调用方。

    返回内核 tag（如 "v1.19.32"）；离线复用缓存且版本未知时返回 ""。
    为什么要回传：fpk 包版本要跟随内核版本（见 manifest_version_for_kernel），
    而内核版本只有这里拿到 /releases/latest 之后才知道。
    """
    log("[1/6] 下载 mihomo 内核...")
    info = ARCH_MAP[arch_key]

    rel = json_api(MIHOMO_API)
    tag = (rel or {}).get("tag_name", "").strip()
    if not tag:
        tag = load_versions().get("mihomo", "")
        if tag:
            warn("网络获取 mihomo 版本失败，回退缓存：%s" % tag)
    if not tag:
        # 连版本号都拿不到（断网）时，若本地已有校验通过的内核就直接用
        cached = os.path.join(BUILD_DIR, "app", "mihomo")
        if os.path.exists(cached) and elf_arch(cached) == info["elf"]:
            warn("无法获取 mihomo 版本号（离线？），复用缓存内核")
            os.makedirs(os.path.join(STAGE_DIR, "app"), exist_ok=True)
            shutil.copy2(cached, os.path.join(STAGE_DIR, "app", "mihomo"))
            return ""
        die("无法获取 mihomo 版本号")

    log("  mihomo 版本：%s" % tag)
    asset = "mihomo-linux-%s-%s.gz" % (info["mihomo"], tag)
    gh = "https://github.com/MetaCubeX/mihomo/releases/download/%s/%s" % (tag, asset)

    dest = os.path.join(STAGE_DIR, "app", "mihomo")
    # ⚠️ 内核缓存放在 .local-build/app/ —— **不在** packroot 里。
    #    packroot 每次构建都会被整个删掉重建；把缓存放里面等于每次重新下载
    #    60MB，且网络抖动时直接失败（实测踩过）。
    cache_bin = os.path.join(BUILD_DIR, "app", "mihomo")
    cache_gz = os.path.join(TMP_DIR, asset)
    os.makedirs(os.path.dirname(cache_bin), exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)

    # 1) 已有校验通过的缓存内核且版本一致 → 直接用
    if not force and os.path.exists(cache_bin) and elf_arch(cache_bin) == info["elf"] \
            and version_match("mihomo", tag):
        log("  复用缓存内核（%d 字节）" % os.path.getsize(cache_bin))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(cache_bin, dest)
        os.chmod(dest, 0o755)
        return tag

    # 2) 下载（压缩包 20-60MB，用断点续传版；实测直连中途掐断是常态）
    # ⚠️ 先试着解压**已有**的 cache_gz：上一次构建被 Ctrl-C / 断电打断时，
    #    这里会留下一个**截断的** .gz。若只看「文件非空 → 跳过下载」，
    #    截断文件会一路走到 gzip.open 抛 EOFError，然后被当成「下载失败」
    #    静默回退到旧缓存内核 —— 明明网络正常却用了旧内核。
    #    所以：解不出来就把坏文件删掉，重新走完整的「直连 → 加速源」流程。
    def _gunzip_gz(path):
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            return None
        try:
            with gzip.open(path, "rb") as f:
                return f.read()
        except Exception as e:
            warn("已有内核压缩包不可用（%s），删除后重新下载" % e)
            try:
                os.remove(path)
            except OSError:
                pass
            return None

    data = None if force else _gunzip_gz(cache_gz)
    if data is None:
        gz_ok = False
        # 直连优先；直连不通再依次走加速源（见 ordered_sources 的说明）
        for full in ordered_sources(gh):
            if read_url_to_file(full, cache_gz):
                gz_ok = True
                break
            log("    ✗ %s 失败，换下一个源" % full)
            # ⚠️ 换源前必须清掉半截文件：否则下一个源会拿上一个源的残片
            #    当「已下载部分」续传，拼出跨源字节垃圾（能过大小的校验）。
            if os.path.exists(cache_gz):
                try:
                    os.remove(cache_gz)
                except OSError:
                    pass
        if not gz_ok:
            warn("内核压缩包下载失败")
        data = _gunzip_gz(cache_gz)

    if data is None:
        # 3) 下载失败 → 复用缓存内核（离线/网络抖动场景）
        if os.path.exists(cache_bin) and elf_arch(cache_bin) == info["elf"]:
            warn("下载失败，复用缓存内核（%d 字节）" % os.path.getsize(cache_bin))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(cache_bin, dest)
            os.chmod(dest, 0o755)
            save_version("mihomo", tag)
            return tag
        die("内核下载失败且本地无可用副本")

    # 校验：必须是目标架构的 ELF，而非错误页或半个文件
    got = None
    if len(data) >= 20 and data[0:4] == b"\x7fELF":
        machine = data[18] | (data[19] << 8)
        got = "x86-64" if machine == 0x3E else ("aarch64" if machine == 0xB7
                                               else "unknown(0x%x)" % machine)
    if got != info["elf"]:
        die("内核架构不符（期望 %s，得到 %s）—— 拒绝打出会崩的包" % (info["elf"], got))

    # 写入缓存 + 暂存区
    with open(cache_bin, "wb") as f:
        f.write(data)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    os.chmod(dest, 0o755)
    log("  ✓ 内核通过校验（%d 字节，%s）" % (len(data), got))
    save_version("mihomo", tag)
    return tag


# ── [2] geo 数据 ─────────────────────────────────────────────

def fetch_geodata(force):
    log("[2/6] 下载 geo 数据...")

    rel = json_api(RULES_API)
    rules_tag = (rel or {}).get("tag_name", "").strip()
    if rules_tag:
        log("  meta-rules-dat 版本：%s" % rules_tag)
    else:
        warn("无法解析 meta-rules-dat 版本号，仅使用 jsDelivr @release 镜像")

    def mirrors_for(name):
        out = []
        if rules_tag:
            out.append("https://github.com/MetaCubeX/meta-rules-dat/releases/"
                       "download/%s/%s" % (rules_tag, name))
        out += [
            "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/" + name,
            "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/" + name,
            "https://gcore.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/" + name,
        ]
        return out

    for kind, name in (("geoip", "geoip.metadb"), ("geosite", "geosite.dat")):
        dest = os.path.join(STAGE_DIR, "app", name)
        # 缓存放 .local-build/geo/ —— 不在 packroot 里，构建时不会被清掉
        geo_cache_dir = os.path.join(BUILD_DIR, "geo")
        os.makedirs(geo_cache_dir, exist_ok=True)
        cache_bin = os.path.join(geo_cache_dir, name)
        cache = os.path.join(TMP_DIR, name + ".dl")
        os.makedirs(TMP_DIR, exist_ok=True)

        ver = rules_tag or "release"
        if not force and geodata_valid(cache_bin, kind) and version_match(kind, ver):
            log("  复用缓存 %s（%d 字节）" % (name, os.path.getsize(cache_bin)))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(cache_bin, dest)
            continue

        ok = False
        # 只有拿到 github.com 的版本化 URL 时才值得套加速源（加速源只代理
        # github 域名，套在 jsDelivr 上没意义），且同样遵循「先直连、后加速」。
        mirrors = mirrors_for(name)
        sources = ordered_sources(mirrors[0]) + mirrors[1:] if rules_tag \
            else list(mirrors)
        for u in sources:
            log("  尝试 %s：%s" % (name, u))
            try:
                data = read_url(u)
            except Exception as e:
                log("    ✗ 下载失败：%s" % e)
                continue
            # 先写临时文件，校验通过才替换 —— 失败的截断文件不能碰到正确文件
            with open(cache, "wb") as f:
                f.write(data)
            if not geodata_valid(cache, kind):
                log("    ✗ 完整性校验不通过（可能是截断文件）")
                continue
            with open(cache_bin, "wb") as f:
                f.write(data)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            log("    ✓ %s 通过完整性校验（%d 字节）" % (name, len(data)))
            save_version(kind, ver)
            ok = True
            break

        if not ok:
            # 全部镜像失败：复用缓存里校验通过的副本（离线构建）
            if geodata_valid(cache_bin, kind):
                warn("所有镜像均失败，复用缓存 %s" % name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(cache_bin, dest)
            else:
                die("%s 下载失败且本地无可用副本 —— 拒绝打出缺 geo 数据的包"
                    "（会让内核启动超时）" % name)


# ── [3] 面板 ─────────────────────────────────────────────────

def _safe_members(t, dest_dir):
    """返回已做越界校验的成员列表。

    同时解决两件事：
      1. 安全：拒绝 ../ 或绝对路径条目（不依赖提取器的默认行为）；
      2. 兼容：Python 3.12+ 提示 3.14 起 extractall 默认会过滤，显式用
         filter='data' 既能消除 DeprecationWarning，行为也向前兼容。
    """
    base = os.path.abspath(dest_dir)
    members = []
    for m in t.getmembers():
        target = os.path.abspath(os.path.join(dest_dir, m.name))
        if target != base and not target.startswith(base + os.sep):
            die("归档条目逃出目标目录：%s" % m.name)
        members.append(m)
    return members, base


def extract_zip(zip_path, dest_dir):
    """用内置 zipfile 解压（含目录穿越防护），跨平台无需外部 unzip。"""
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.abspath(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as z:
        for name in z.namelist():
            target = os.path.abspath(os.path.join(dest_dir, name))
            if target != base and not target.startswith(base + os.sep):
                die("zip 条目逃出目标目录：%s" % name)
        z.extractall(dest_dir)


def extract_targz(tgz_path, dest_dir):
    """用内置 tarfile 解压（含目录穿越防护）。"""
    os.makedirs(dest_dir, exist_ok=True)
    with tarfile.open(tgz_path, "r:gz") as t:
        _safe_members(t, dest_dir)
        try:
            t.extractall(dest_dir, filter="data")   # py3.12+
        except TypeError:                            # py<3.12 无 filter 参数
            t.extractall(dest_dir)


def dir_stats(path):
    files = 0
    size = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            files += 1
            size += os.path.getsize(os.path.join(root, n))
    return files, size


def download_asset(candidates, out_file, description, force=False):
    """按候选 URL 顺序下载，任一成功即可；已缓存则跳过。

    候选列表里同时给 `releases/latest/download/...`（滚动最新）和带版本号的
    固定 URL。实测 `latest` 的 302 跳转在部分网络下更容易超时，多了固定 URL
    这条退路能明显提高成功率。

    对**每个**候选 URL 都走「先直连、后加速源」，并带单源超时：
    这样既能治「latest 的 302 跳转超时」，也能治「加速源临时挂掉」。
    """
    if not force and os.path.exists(out_file) and os.path.getsize(out_file) > 0:
        log("  复用缓存 %s（%d 字节）" % (description, os.path.getsize(out_file)))
        return True

    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    tmp = out_file + ".part"
    last = ""
    ordered = [s for u in candidates for s in ordered_sources(u)]
    for full in ordered:
        try:
            log("    尝试 %s" % full[:110])
            data = read_url(full)
            if not data:
                last = "内容为空"
                continue
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out_file)
            log("  ✓ %s（%d 字节）" % (description, len(data)))
            return True
        except Exception as e:
            last = str(e)
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    log("  ✗ %s 下载失败：%s" % (description, last))
    return False


def fetch_panels(force):
    log("[3/6] 下载管理面板...")

    # 解析两个面板的版本号，用于拼「带版本号的固定 URL」作为回退源。
    # 实测 `releases/latest/download/...` 的 302 跳转在部分网络下容易超时。
    global MCX_TAG, ZASH_TAG
    MCX_TAG = ((json_api("https://api.github.com/repos/MetaCubeX/metacubexd/"
                         "releases/latest") or {}).get("tag_name") or "").strip()
    ZASH_TAG = ((json_api("https://api.github.com/repos/Zephyruso/zashboard/"
                          "releases/latest") or {}).get("tag_name") or "").strip()
    if MCX_TAG:
        log("  MetaCubeXD 版本：%s" % MCX_TAG)
    if ZASH_TAG:
        log("  Zashboard 版本：%s" % ZASH_TAG)

    # 面板归档缓存在 .local-build/panels/ —— 不在 packroot 里
    pdir = os.path.join(BUILD_DIR, "panels")
    os.makedirs(pdir, exist_ok=True)

    # MetaCubeXD：compressed-dist.tgz
    # ⚠️ 其 Release 里还有 100MB+ 的 .deb/.AppImage/.exe —— 那是 Electron 桌面
    #    客户端，**不是**要打进 fpk 的东西。要的是 compressed-dist.tgz。
    mcx_dir = os.path.join(STAGE_DIR, "app", "panels", "metacubexd")
    if os.path.exists(mcx_dir):
        shutil.rmtree(mcx_dir)
    os.makedirs(mcx_dir, exist_ok=True)
    tgz = os.path.join(pdir, "metacubexd.tgz")
    mcx_candidates = [
        "https://github.com/MetaCubeX/metacubexd/releases/latest/download/compressed-dist.tgz",
    ]
    if MCX_TAG:
        mcx_candidates.append(
            "https://github.com/MetaCubeX/metacubexd/releases/download/%s/"
            "compressed-dist.tgz" % MCX_TAG)
    if not download_asset(mcx_candidates, tgz, "MetaCubeXD compressed-dist.tgz", force):
        die("MetaCubeXD 下载失败")
    extract_targz(tgz, mcx_dir)
    f, s = dir_stats(mcx_dir)
    log("  MetaCubeXD：%d 个文件，%.1f MB" % (f, s / 1048576))

    # Zashboard：dist-no-fonts.zip（内层是 dist/，需提升一层）
    zash_dir = os.path.join(STAGE_DIR, "app", "panels", "zashboard")
    if os.path.exists(zash_dir):
        shutil.rmtree(zash_dir)
    os.makedirs(zash_dir, exist_ok=True)
    zpath = os.path.join(pdir, "zashboard.zip")
    zash_candidates = [
        "https://github.com/Zephyruso/zashboard/releases/latest/download/dist-no-fonts.zip",
    ]
    if ZASH_TAG:
        zash_candidates.append(
            "https://github.com/Zephyruso/zashboard/releases/download/%s/"
            "dist-no-fonts.zip" % ZASH_TAG)
    if not download_asset(zash_candidates, zpath, "Zashboard dist-no-fonts.zip", force):
        die("Zashboard 下载失败")
    raw_dir = os.path.join(TMP_DIR, "zash-x")
    extract_zip(zpath, raw_dir)
    inner = os.path.join(raw_dir, "dist")
    if not os.path.isdir(inner):
        inner = raw_dir
    for name in os.listdir(inner):
        copy_tree(os.path.join(inner, name), os.path.join(zash_dir, name))
    f, s = dir_stats(zash_dir)
    log("  Zashboard：%d 个文件，%.1f MB" % (f, s / 1048576))


# ── [3c] MetaCubeXD 后端注入 ─────────────────────────────────

MCX_CONFIG_JS = """// 由 mihomo-fnos 构建脚本写入。
// defaultBackendURL 必须是完整 URL；这里运行时用 location.origin 拼出同源地址，
// 因此 HTTPS 打开时不会触发 mixed content，也无需用户手填。
(function () {
  var base = '';
  try {
    // config.js 位于 /app/<appname>/panel/metacubexd/，从当前路径反推应用根，
    // 避免写死 appname（也兼容网关前缀变化）
    var m = location.pathname.match(/^(.*?)\\/panel\\/[^/]+\\/?/);
    var prefix = m && m[1] ? m[1] : '/app/mihomo';
    base = location.origin + prefix;
  } catch (e) { base = ''; }
  window.__METACUBEXD_CONFIG__ = window.__METACUBEXD_CONFIG__ || {};
  window.__METACUBEXD_CONFIG__.defaultBackendURL = base;
  window.__METACUBEXD_CONFIG__.githubToken = '';
})();
"""


def patch_metacubexd():
    """写入运行时同源后端地址。

    ⚠️ 必须是【完整 URL】（以 http:// 或 https:// 开头）。
       MetaCubeXD 内部逻辑：e.startsWith('http://')||e.startsWith('https://')
                            ? e : `${location.protocol}//${e}`
       所以写 '/app/mihomo' 会被拼成 'http://app/mihomo'（host 变成 "app"）→ 连不上。
    ⚠️ 也不能写死域名（fnOS 域名/局域网 IP/反代域名各环境不同），
       故运行时用 location.origin 拼同源完整 URL —— HTTPS 下也不会 mixed content。
    """
    mcx_dir = os.path.join(STAGE_DIR, "app", "panels", "metacubexd")

    cfg = os.path.join(mcx_dir, "config.js")
    if os.path.exists(cfg):
        with open(cfg, "w", encoding="utf-8", newline="") as f:
            f.write(MCX_CONFIG_JS)
        log("  已写入 MetaCubeXD config.js（运行时同源完整 URL）")

    # index.html 里硬编码了 defaultBackendURL（Nuxt config + fallback）。
    # 运行时优先级：① r.public.defaultBackendURL（非空则优先）② config.js。
    # ① 非空会盖掉 config.js 的动态值 → 必须全部置空，强制回退到 config.js。
    idx = os.path.join(mcx_dir, "index.html")
    if not os.path.exists(idx):
        return
    with open(idx, "r", encoding="utf-8") as f:
        html = f.read()
    orig = html
    html = html.replace('defaultBackendURL:"/app/mihomo"', 'defaultBackendURL:""')
    html = html.replace("defaultBackendURL:'/app/mihomo'", "defaultBackendURL:''")
    if html != orig:
        with open(idx, "w", encoding="utf-8", newline="") as f:
            f.write(html)
        log("  index.html 的 defaultBackendURL 已置空（回退到 config.js）")
    if re.search(r'defaultBackendURL:"[^"]+"', html):
        warn("index.html 仍存在非空 defaultBackendURL，可能盖掉 config.js")


# ── [3d] Zashboard 后端注入 ──────────────────────────────────

ZASH_INJECT = """    <script>
      ;(function () {
        try {
          var KEY = 'setup/api-list';
          var existing = [];
          try { existing = JSON.parse(localStorage.getItem(KEY) || '[]') || []; } catch (e) {}
          var m = location.pathname.match(/^(.*?)\\/panel\\/[^/]+\\/?/);
          var prefix = (m && m[1]) ? m[1] : '/app/mihomo';
          var want = {
            protocol: location.protocol.replace(':', ''),
            host: location.hostname,
            port: location.port || (location.protocol === 'https:' ? '443' : '80'),
            secondaryPath: prefix, type: 'clash', label: 'Mihomo (\\u672c\\u673a)'
          };
          var dup = existing.some(function (it) {
            return it && it.host === want.host && (it.secondaryPath || '') === prefix;
          });
          if (!dup) {
            want.uuid = 'mihomo-local-' + Math.random().toString(36).slice(2, 10);
            existing.push(want);
            localStorage.setItem(KEY, JSON.stringify(existing));
            localStorage.setItem('setup/active-uuid', want.uuid);
          }
        } catch (e) {}
      })();
    </script>
"""


def patch_zashboard():
    """预置 localStorage['setup/api-list']。

    Zashboard 无 config.js 注入点，后端列表只在 localStorage（默认 []）；
    首次打开只显示「请添加后端」且**不得发任何请求**，所以必须构建期预置。
    其 URL 构造为 `${protocol}://${host}:${port}${secondaryPath}`，
    故 secondaryPath 必须填应用根（如 /app/mihomo）。
    """
    idx = os.path.join(STAGE_DIR, "app", "panels", "zashboard", "index.html")
    if not os.path.exists(idx):
        return
    with open(idx, "r", encoding="utf-8") as f:
        html = f.read()
    if "setup/api-list" in html:
        log("  Zashboard 后端预置已存在，跳过注入")
        return
    m = re.search(r'\n\s*<script type="module"', html)
    if not m:
        warn("未找到应用脚本标签，跳过 Zashboard 注入")
        return
    html = html[:m.start()] + "\n" + ZASH_INJECT + html[m.start():]
    with open(idx, "w", encoding="utf-8", newline="") as f:
        f.write(html)
    log("  已注入 Zashboard 同源后端预置")


# ── [4] 面板内切换入口 + sw 版本刷新 ─────────────────────────

BODY_CLOSE_RE = re.compile(r"</body>", re.I)
SW_ENTRY_RE = re.compile(r'(\{url:"(?:index\.html|\./)",revision:")([0-9a-f]{32})(")')


def refresh_sw_revision(index_path, sw_path, label):
    """把 sw.js 里 index.html 条目的 revision 改成该文件的真实 MD5。

    只改这一条：其余条目要么是 revision:null 的内容哈希资源（无需校验），
    要么是未改动的文件，动了反而让客户端白下载。

    Workbox 的 revision 算法就是 MD5（已实证：registerSW.js 的 revision
    等于该文件真实 MD5，MetaCubeXD 的 "./" 条目同理）。
    """
    if not (os.path.exists(index_path) and os.path.exists(sw_path)):
        warn("%s：缺少 index.html 或 sw.js，跳过 sw 版本刷新" % label)
        return
    import hashlib
    with open(index_path, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()
    with open(sw_path, "r", encoding="utf-8") as f:
        src = f.read()
    m = SW_ENTRY_RE.search(src)
    if not m:
        warn("%s：sw.js 中未找到 index.html 预缓存条目" % label)
        return
    if m.group(2) == md5:
        log("  %s：sw 预缓存版本已是最新，跳过" % label)
        return
    out = src[:m.start(2)] + md5 + src[m.end(2):]
    with open(sw_path, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    log("  %s：sw 预缓存版本已刷新 -> %s" % (label, md5))


def inject_panel_switch():
    """把 scripts/panel-switch-boot.js 包进 <script> 插到 </body> 之前。

    为什么不能靠「在面板地址后面加 ?choose=1」：两个面板都是 PWA，各自注册了
    Workbox 的 NavigationRoute（绑定到预缓存的 index.html），面板内的任何导航
    都被 SW 用缓存应答，请求到不了服务端 —— 而 ?choose=1 只有 chooser.html 会读。
    故切换入口必须活在**面板页面自身的 DOM** 里。

    ⚠️ 注入 HTML 后**必须**同步更新 sw.js 的 revision，否则改动对老用户永久不可见
       （sw.js 字节没变 → 浏览器不更新 SW → 一直用缓存里的旧 index.html）。
    """
    log("[4/6] 注入面板内切换入口...")

    boot_path = os.path.join(PROJECT_DIR, "scripts", "panel-switch-boot.js")
    if not os.path.exists(boot_path):
        die("缺少引导脚本模板 %s" % boot_path)
    with open(boot_path, "r", encoding="utf-8") as f:
        boot = f.read()
    # 防御：内联脚本里出现结束标签字面量会提前闭合 <script>，破坏整个页面。
    if "</script" in boot.lower():
        die("引导脚本 %s 中含 </script>，会破坏页面 —— 拒绝注入" % boot_path)

    for name, label in (("metacubexd", "MetaCubeXD"), ("zashboard", "Zashboard")):
        d = os.path.join(STAGE_DIR, "app", "panels", name)
        idx = os.path.join(d, "index.html")
        if not os.path.exists(idx):
            warn("%s：未找到 index.html，跳过切换入口注入" % label)
            continue
        with open(idx, "r", encoding="utf-8") as f:
            html = f.read()
        if "panel-switch.js" in html:
            log("  %s：切换入口已存在，跳过注入" % label)
            continue
        m = BODY_CLOSE_RE.search(html)
        if not m:
            warn("%s：未找到 </body>，跳过切换入口注入" % label)
            continue
        html = html[:m.start()] + "<script>\n" + boot + "</script>\n" + html[m.start():]
        with open(idx, "w", encoding="utf-8", newline="") as f:
            f.write(html)
        log("  %s：切换入口已注入" % label)
        refresh_sw_revision(idx, os.path.join(d, "sw.js"), label)


# ── [0] 暂存目录 ─────────────────────────────────────────────

# ⚠️ 不要用 POSIX 字符类 [[:space:]]：那是 grep/sed 的语法，Python re 不支持，
#    会被当成嵌套字符集（FutureWarning: Possible nested set），匹配结果不可靠。
PLATFORM_RE = re.compile(r"(?m)^platform\s*=.*$")
ARCH_RE = re.compile(r"(?m)^arch\s*=.*$")
VERSION_RE = re.compile(r"(?m)^version\s*=.*$")


def stage_project_files(arch_key, version):
    """把项目文件拷进暂存目录，并写好 manifest 的 platform/version。

    ⚠️ 与原 bash 脚本的关键区别：原脚本**就地修改源码树 app/**，会把下载的面板、
       注入后的 HTML 留在源码里（既脏、又让重建不可复现）。这里改为暂存到
       .local-build/packroot，源码树保持干净。
    """
    log("[0/6] 准备暂存目录...")
    if os.path.exists(STAGE_DIR):
        shutil.rmtree(STAGE_DIR)
    for d in ("cmd", "config", "wizard", "app/ui", "app/panels"):
        os.makedirs(os.path.join(STAGE_DIR, d), exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)

    for sub in ("cmd", "config", "wizard"):
        src = os.path.join(PROJECT_DIR, sub)
        if os.path.isdir(src):
            copy_tree(src, os.path.join(STAGE_DIR, sub))
    for icon in ("ICON.PNG", "ICON_256.PNG"):
        p = os.path.join(PROJECT_DIR, icon)
        if os.path.exists(p):
            shutil.copy2(p, STAGE_DIR)

    for ui_sub in ("config", "images", "index.html", "panel-switch.js"):
        p = os.path.join(PROJECT_DIR, "app", "ui", ui_sub)
        if os.path.exists(p):
            copy_tree(p, os.path.join(STAGE_DIR, "app", "ui", os.path.basename(ui_sub)))

    # app/ 下的其它源文件（含 ui_server.py —— 必须在 app/ 内才会被打包）
    app_src = os.path.join(PROJECT_DIR, "app")
    for name in os.listdir(app_src):
        if name in ("ui", "panels"):
            continue
        copy_tree(os.path.join(app_src, name), os.path.join(STAGE_DIR, "app", name))

    # panels/chooser.html（静态选择页，非下载产物）
    chooser = os.path.join(app_src, "panels", "chooser.html")
    if os.path.exists(chooser):
        copy_tree(chooser, os.path.join(STAGE_DIR, "app", "panels", "chooser.html"))

    write_manifest(os.path.join(STAGE_DIR, "manifest"), arch_key, version)
    with open(os.path.join(STAGE_DIR, "manifest"), "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("platform"):
                log("  " + line.strip())
                break
    log("  暂存目录：%s" % STAGE_DIR)


def write_manifest(dest, arch_key, version):
    """写 manifest：设置 platform / version，清掉已废弃的 arch 字段。

    ⚠️ platform 必须在**下载之前**写对。若放到最后，一旦下载失败/中断，
       会留下「manifest 声明架构」与「包内二进制架构」错位的包
       （实测踩过：aarch64 内核 + platform=x86，装到目标机必崩）。
    """
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    plat_line = "platform                   = %s" % ARCH_MAP[arch_key]["manifest"]
    if PLATFORM_RE.search(content):
        content = PLATFORM_RE.sub(plat_line, content)
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += plat_line + "\n"

    if ARCH_RE.search(content):
        warn("移除已废弃的 arch 字段（官方改用 platform）")
        content = ARCH_RE.sub("", content)

    if VERSION_RE.search(content):
        content = VERSION_RE.sub("version = %s" % version, content)
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += "version = %s\n" % version

    content = re.sub(r"\n{3,}", "\n\n", content)
    with open(dest, "w", encoding="utf-8", newline="") as f:
        f.write(content)


# ── [5] 打包前自检 ───────────────────────────────────────────

def verify_stage(arch_key):
    log("[5/6] 打包前自检...")
    info = ARCH_MAP[arch_key]

    for rel in ("manifest", "config/privilege", "config/resource",
                "ICON.PNG", "ICON_256.PNG", "app", "cmd", "wizard"):
        if not os.path.exists(os.path.join(STAGE_DIR, rel)):
            die("缺少必需文件/目录：%s" % rel)
    for rel in ("config/privilege", "config/resource"):
        with open(os.path.join(STAGE_DIR, rel), "r", encoding="utf-8") as f:
            try:
                json.load(f)
            except Exception as e:
                die("%s 不是合法 JSON：%s" % (rel, e))

    kernel = os.path.join(STAGE_DIR, "app", "mihomo")
    if not os.path.exists(kernel):
        die("app/mihomo 不存在")
    got = elf_arch(kernel)
    if got != info["elf"]:
        die("app/mihomo 架构不符（期望 %s，得到 %s）—— 拒绝打包"
            % (info["elf"], got))
    log("  架构一致：内核 %s / platform=%s" % (got, info["manifest"]))

    # geo 数据硬门禁：损坏的 geoip.metadb 绝不能进包
    #（2026-10-03 事故即因缺少此门禁，截断文件被打包 → 装机后内核启动超时）
    for kind, name in (("geoip", "geoip.metadb"), ("geosite", "geosite.dat")):
        p = os.path.join(STAGE_DIR, "app", name)
        if not geodata_valid(p, kind):
            die("app/%s 缺失或已损坏 —— 拒绝打包"
                "（会导致内核启动时联网拉取 geo 数据并超时）" % name)
        log("  geo 校验通过：%s（%d 字节）" % (name, os.path.getsize(p)))

    for p in ("metacubexd", "zashboard"):
        idx = os.path.join(STAGE_DIR, "app", "panels", p, "index.html")
        if not os.path.exists(idx):
            die("面板 %s 缺 index.html" % p)
        with open(idx, "r", encoding="utf-8") as f:
            if "panel-switch.js" not in f.read():
                die("面板 %s 未注入切换入口" % p)

    # 【最后防线】marker 残留检查：构建期的 .part 文件不能进包
    for root, _dirs, names in os.walk(STAGE_DIR):
        for n in names:
            if n.endswith(".part") or n.endswith(".dl"):
                die("暂存目录残留临时文件：%s" % os.path.join(root, n))


# ── [6] 打包 + 修权限 ────────────────────────────────────────

def ensure_fnpack(force):
    url = get_fnpack_url()
    name = url.rsplit("/", 1)[-1]
    if get_platform() == "windows":
        name += ".exe"
    path = os.path.join(BUILD_DIR, name)

    if not force and os.path.exists(path) and os.path.getsize(path) > 0 \
            and version_match("fnpack", FNPACK_VER):
        log("  使用缓存 fnpack %s" % FNPACK_VER)
        return path

    # fnpack 走**纯直连**，不套加速源：
    #   - static2.fnnas.com 是飞牛自家静态站，不是 GitHub 资产，本来就不该
    #     绕第三方前缀代理（那些加速源是为 GitHub 设计的）；
    #   - 多套一层只会多一个失败点、多一轮超时等待，收益为负；
    #   - 它是打包工具本身，下载失败即全盘失败，所以给更宽的超时
    #     （FNPACK_TIMEOUT，默认 120s）。
    # 传 mirrors=[] 即「只用直连」。
    if not download(url, path, "fnpack", "fnpack", FNPACK_VER, force,
                    mirrors=[], timeout=FNPACK_TIMEOUT):
        die("fnpack 下载失败（%s）" % url)
    if get_platform() != "windows":
        os.chmod(path, 0o755)
    return path


def _rewrite_tar_modes(raw, outer_pattern, inner_pattern=None, fixed_mtime=None):
    """重建 tar：给匹配 outer_pattern 的成员设 0755；app.tgz 递归处理内部。

    这是 Windows 打包的**必需**修复：实测 Windows 版 fnpack 把 cmd/* 写成 0666
    （目录 0777），装机后生命周期脚本不可执行、升级回调 `-x $MAIN_SCRIPT` 失败。
    在 Linux 上 fnpack 会正确写 0755，故此函数在 Linux 上是幂等的。

    另：`fixed_mtime` 非空时把所有成员时间戳钉成同一值。tar 里带的是**构建那一刻**
    的 mtime，否则同样的输入每次都会产出不同的 `.fpk` 字节（内容其实一致）。
    钉住后构建可复现，sha256 才有比对价值。
    """
    src = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    out_buf = io.BytesIO()
    dst = tarfile.open(fileobj=out_buf, mode="w", format=tarfile.USTAR_FORMAT)

    for m in src.getmembers():
        if not m.isreg():
            # 目录/链接也要归一化：tar 里目录项**带 mtime**，若原样透传，
            # 即便文件内容全钉住，app.tgz 字节仍会随构建时刻变化，
            # 进而改动 manifest.checksum（fnpack 会算 app.tgz 的 MD5）。
            # 实测正是漏了这一步导致「内容一致但 sha256 每次不同」。
            ti = tarfile.TarInfo(m.name)
            ti.mode = 0o755 if m.isdir() else m.mode
            ti.size = 0
            ti.mtime = fixed_mtime if fixed_mtime is not None else m.mtime
            ti.type = m.type
            ti.linkname = getattr(m, "linkname", "") or ""
            ti.uname = ""
            ti.gname = ""
            ti.uid = 0
            ti.gid = 0
            dst.addfile(ti)
            continue
        data = src.extractfile(m).read()
        if m.name == "app.tgz" and inner_pattern is not None:
            inner_raw = gzip.decompress(data)
            inner_fixed = _rewrite_tar_modes(inner_raw, inner_pattern, None,
                                             fixed_mtime)
            # mtime=0：gzip 头里也带时间戳，不钉住同样会破坏可复现性
            data = gzip.compress(inner_fixed, compresslevel=6, mtime=0)
        elif outer_pattern.match(m.name):
            m.mode = 0o755
        ti = tarfile.TarInfo(m.name)
        ti.mode = m.mode
        ti.size = len(data)
        ti.mtime = fixed_mtime if fixed_mtime is not None else m.mtime
        ti.type = m.type
        ti.uname = ""
        ti.gname = ""
        ti.uid = 0
        ti.gid = 0
        dst.addfile(ti, io.BytesIO(data))

    dst.close()
    src.close()
    return out_buf.getvalue()


def fix_fpk_permissions(fpk_path):
    """修正 .fpk 内 cmd/* 与 app.tgz 内可执行文件（内核/py 脚本）的权限。

    必须做：Windows 打包产物里这些是 0666，装到 fnOS 上不可执行。

    同时把所有成员的时间戳钉成同一值，使「同样的输入 → 同样的 .fpk 字节」。
    时间取 `SOURCE_DATE_EPOCH`（若设置，便于可复现构建）否则取 0，保证跨机器
    跨次构建结果一致 —— 这样产物 sha256 才有比对意义。
    只走**一次**重写（此前分三次重写，既慢又容易前后不一致）。
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    fixed_mtime = int(epoch) if (epoch or "").strip().isdigit() else 0

    with gzip.open(fpk_path, "rb") as f:
        raw = f.read()

    # 记录修正前的 cmd/* 权限，用于报告
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as t:
        outer_before = {m.name: m.mode for m in t.getmembers()
                        if m.isreg() and m.name.startswith("cmd/")}

    exe_re = re.compile(r"^(mihomo|bin/[^/]+(\.py)?|panels/.*\.(cgi|sh))$")
    fixed = _rewrite_tar_modes(raw, re.compile(r"^cmd/[^/]+$"),
                              inner_pattern=exe_re, fixed_mtime=fixed_mtime)
    # 外层 gzip 头的时间戳同样要钉住
    with open(fpk_path, "wb") as f:
        f.write(gzip.compress(fixed, compresslevel=6, mtime=0))

    # ── 重算 manifest.checksum ────────────────────────────────────
    # ⚠️ 真实缺陷：fnpack 是在**改写之前**算的 checksum（app.tgz 的 MD5）。
    # 我们改了 app.tgz（权限位 + 钉时间戳），字节变了，于是 manifest 里的
    # checksum 与**实际发出去的那个 app.tgz** 对不上。安装器若校验该字段
    # 就会判定包损坏。这里按最终 app.tgz 重新计算并回写，保证自洽。
    with tarfile.open(fileobj=io.BytesIO(fixed), mode="r:") as t:
        app_tgz = t.extractfile("app.tgz").read()
        checksum = hashlib.md5(app_tgz).hexdigest()
        members = t.getmembers()
        man_member = next((x for x in members if x.name == "manifest"), None)
        man_text = (t.extractfile(man_member).read().decode("utf-8")
                    if man_member is not None else None)
    if man_member is not None:
        man_text = man_text or ""
        new_man = re.sub(r"(?m)^checksum\s*=.*$",
                         "checksum                   = " + checksum, man_text)
        if new_man == man_text:
            new_man = man_text.rstrip("\n") + "\nchecksum                   = " + checksum + "\n"
        # 用最终 manifest 重建整包（也顺便钉住时间戳）
        # 注意：这里要重新读一遍 members（上面 with 块已退出，句柄不可再用）
        with tarfile.open(fileobj=io.BytesIO(fixed), mode="r:") as tsrc:
            out_buf = io.BytesIO()
            dst = tarfile.open(fileobj=out_buf, mode="w", format=tarfile.USTAR_FORMAT)
            for ti in tsrc.getmembers():
                data = tsrc.extractfile(ti).read() if ti.isreg() else b""
                if ti.name == "manifest":
                    data = new_man.encode("utf-8")
                nt = tarfile.TarInfo(ti.name)
                nt.mode = 0o755 if ti.isdir() else ti.mode
                nt.size = len(data)
                nt.mtime = fixed_mtime
                nt.type = ti.type
                nt.linkname = ti.linkname or ""
                nt.uname = nt.gname = ""
                nt.uid = nt.gid = 0
                dst.addfile(nt, io.BytesIO(data) if (ti.isreg() or ti.name == "manifest")
                            else None)
            dst.close()
        fixed = out_buf.getvalue()
        log("  已按最终 app.tgz 重算 manifest.checksum=%s" % checksum[:16])
    else:
        warn("未找到 manifest，跳过 checksum 重算")
    # 报告 cmd/* 权限修正情况
    changed = []
    with tarfile.open(fileobj=io.BytesIO(fixed), mode="r:") as t:
        for m in t.getmembers():
            if m.isreg() and m.name.startswith("cmd/"):
                before = outer_before.get(m.name)
                if before is not None and before != m.mode:
                    changed.append("%s %s->%s" % (m.name, oct(before), oct(m.mode)))
    if changed:
        log("  已修正 %d 个 cmd/* 执行位（Windows 打包会写成 0666）" % len(changed))
    else:
        log("  cmd/* 权限无需修正")
    log("  已钉住包内时间戳（mtime=%d），构建可复现" % fixed_mtime)


def build_fpk(force):
    log("[6/6] 调用 fnpack 打包...")
    fnpack = ensure_fnpack(force)

    out_cache = os.path.join(STAGE_DIR, "mihomo.fpk")
    if os.path.exists(out_cache):
        os.remove(out_cache)

    log("  Running fnpack build...")
    # ⚠️ fnpack 会把临时目录建在 %TEMP%。若 %TEMP% 位于受限/不可写位置
    #    （受限账户、沙箱、或 C: 盘配额问题），会直接报
    #    "Create tmp dir ... Access is denied" 而打包失败。
    #    这里显式把子进程的 TEMP/TMP 指到本项目的 .local-build/tmp（必然可写、
    #    且与工作目录同盘），彻底规避该环境问题。
    child_env = dict(os.environ)
    child_env["TEMP"] = TMP_DIR
    child_env["TMP"] = TMP_DIR
    os.makedirs(TMP_DIR, exist_ok=True)
    proc = subprocess.run([fnpack, "build", "."], cwd=STAGE_DIR,
                          capture_output=True, env=child_env)
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")
    for line in stdout.splitlines():
        log("    " + line)

    if not os.path.exists(out_cache):
        log("ERROR: fnpack build 失败")
        if stderr:
            log("  " + stderr[:2000])
        sys.exit(1)

    # Windows 上 fnpack 把所有文件写成 0666（无执行位），必须修
    fix_fpk_permissions(out_cache)

    final_name = "mihomo-%s-%s.fpk" % (APP_VERSION, APP_ARCH)
    final_path = os.path.join(PROJECT_DIR, final_name)
    if os.path.exists(final_path):
        os.remove(final_path)
    shutil.move(out_cache, final_path)

    import hashlib
    with open(final_path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    with open(final_path + ".sha256", "w", encoding="utf-8", newline="") as f:
        f.write("%s  %s\n" % (sha, final_name))

    log("  %s（%.1f MB）" % (final_name, os.path.getsize(final_path) / 1048576))
    log("  SHA256: %s" % sha)
    return final_path


# ── main ─────────────────────────────────────────────────────

APP_VERSION = ""
APP_ARCH = ""
MCX_TAG = ""
ZASH_TAG = ""


def main():
    global APP_VERSION, APP_ARCH

    # 必须放在任何 log() 之前：Windows 控制台默认 GBK，日志里的 ✓ 会直接崩
    _force_utf8_stdout()

    parser = argparse.ArgumentParser(description="mihomo fnOS 应用打包脚本")
    parser.add_argument("--arch", "-a", default="x86", choices=["x86", "arm"],
                        help="目标架构（默认 x86）")
    parser.add_argument("--version", "-v", default="",
                        help="版本号（默认跟随 mihomo 内核版本，如 v1.19.32 → 1.19.32；"
                             "内核版本取不到时回退读 manifest）")
    parser.add_argument("--force", "-f", action="store_true",
                        help="强制重新下载所有依赖")
    args = parser.parse_args()

    APP_ARCH = args.arch
    force = args.force
    info = ARCH_MAP[APP_ARCH]
    log("========================================")
    log("  fnOS mihomo - 构建")
    log("  开发机：  %s/%s" % (get_platform(), get_platform_arch()))
    log("  目标架构：%s（内核 %s，platform=%s）"
        % (APP_ARCH, info["mihomo"], info["manifest"]))
    log("========================================")

    # ── 版本号与 stage 的先后顺序（踩过坑，别随手调换）─────────────
    # 约束一：platform 必须在**下载之前**写进 manifest，否则下载失败时
    #         会留下「manifest 声明架构」与「包内二进制」错位的包。
    # 约束二：包版本要跟随内核版本，而内核版本要等 fetch_kernel 请求
    #         /releases/latest 之后才知道。
    # 两者冲突，所以拆成两步：先 stage（platform 就位、version 放占位/旧值），
    # 拿到内核版本后再**重写一次** manifest 的 version。
    # 重写发生在 verify_stage / build_fpk **之前**，故最终产物版本必然正确。
    stage_project_files(APP_ARCH, read_manifest_version() or "0.0.0")

    kernel_tag = fetch_kernel(APP_ARCH, force)

    # 显式 --version 优先级最高；否则跟随内核版本；内核版本拿不到/不合法
    # （离线、或 tag 形如 alpha-xxx）才回退 manifest 里手写的版本号。
    if args.version.strip():
        APP_VERSION = args.version.strip()
        log("  包版本：  %s（来自 --version）" % APP_VERSION)
    else:
        APP_VERSION = kernel_version_to_app_version(kernel_tag)
        if APP_VERSION:
            log("  包版本：  %s（跟随内核 %s）" % (APP_VERSION, kernel_tag))
        else:
            APP_VERSION = read_manifest_version()
            if not APP_VERSION:
                die("无法确定版本号：内核 tag=%r 不可用，manifest 也没有 version"
                    % kernel_tag)
            warn("内核版本 %r 无法转成包版本，回退 manifest：%s"
                 % (kernel_tag, APP_VERSION))
    # 把定稿的 version 写回暂存 manifest（platform 已在上一步写对，保持不变）
    write_manifest(os.path.join(STAGE_DIR, "manifest"), APP_ARCH, APP_VERSION)

    fetch_geodata(force)
    fetch_panels(force)
    patch_metacubexd()
    patch_zashboard()
    inject_panel_switch()
    verify_stage(APP_ARCH)
    final = build_fpk(force)

    log("")
    log("========================================")
    log("  构建完成：%s" % os.path.basename(final))
    log("========================================")


if __name__ == "__main__":
    main()
