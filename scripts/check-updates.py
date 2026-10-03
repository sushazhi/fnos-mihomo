#!/usr/bin/env python3
"""
check-updates.py - 探测上游（mihomo 内核 / MetaCubeXD / Zashboard）是否有新版本。

用途：给 GitHub Actions 的「每周检查」用，也**可以本地直接跑**看当前落后多少。
只用标准库，不依赖 requests；下载走与 build.py 相同的「先直连、后加速源」策略。

输出：
    - 人类可读的对比表 → stdout
    - 机器可读的 key=value → 追加到 $GITHUB_OUTPUT（在 CI 里时），或 --out 指定文件

版本号规则（重要）：
    包版本 = <内核版本>              ← 内核更新时，如 1.19.32
    包版本 = <内核版本>.<序号>        ← 内核没变、只有面板更新时，如 1.19.32.1
                                        同一内核基线下逐次递增：.1 → .2 → …
    为什么用「第四段数字」而不是 `-r2` 这类字母后缀：
        官方 manifest 文档只说 version 形如 `1.0.0` / `2.1.3-beta`，
        **没有说明版本如何比较大小**。若飞牛按 semver 规则比较，`-r2` 属于
        pre-release，会**小于** `1.19.32`，设备会当成降级而不发更新。
        纯数字点分 `1.19.32.1` 在「字符串比较」和「数字段比较」两种规则下
        都大于 `1.19.32`，排序无歧义，故采用。
    内核一变，序号归零（回到 `1.19.32` 本身），因为 `1.20.0 > 1.19.32.5`。

状态存放：
    「上次打包用的哪个面板版本」记在 scripts/pinned-versions.json，**不写进
    manifest**。fnpack 是 schema 驱动的，往 manifest 塞自定义键有被拒或静默
    丢弃的风险；这个文件不参与打包，fnOS 看不到它。

关键设计：
    * **不把「探测失败」当成「没有更新」。** 上游 API 挂了 / 被限流 / 断网时，
      探测失败会以非 0 退出码结束并打印原因，让 workflow 明确失败，
      而不是静默认为「无更新」—— 否则会长期不发新版却显示绿灯。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# 与 build.py 保持一致的下载策略（先直连，失败再走加速源）
DOWNLOAD_TIMEOUT = int(os.environ.get("MIHOMO_DL_TIMEOUT", "60"))
_DEFAULT_MIRRORS = ["https://gh.dpik.top/", "https://gh-proxy.org/"]

# 状态文件：相对本脚本所在目录
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "pinned-versions.json")

# 监控目标：key -> (owner/repo, 人类可读名)
TARGETS = [
    ("mihomo", "MetaCubeX/mihomo", "mihomo 内核"),
    ("metacubexd", "MetaCubeX/metacubexd", "MetaCubeXD 面板"),
    ("zashboard", "Zephyruso/zashboard", "Zashboard 面板"),
]

RELEASE_API = "https://api.github.com/repos/%s/releases/latest"


def _mirrors():
    env = os.environ.get("MIHOMO_DL_MIRRORS")
    if env is None:
        return list(_DEFAULT_MIRRORS)
    return [m.strip().rstrip("/") + "/" for m in env.split(",") if m.strip()]


def http_json(url, timeout=None):
    """取 JSON。返回 (data, None) 或 (None, 错误字符串)。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "mihomo-fnos-check/1.0",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout or DOWNLOAD_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s" % e.code
    except Exception as e:
        return None, str(e)


def latest_tag(repo):
    """查某仓库 latest release 的 tag。

    直连失败自动退到加速源（同 build.py 策略）。注意 GitHub API 走加速源
    常被共享 IP 的限流打到 403，所以逐个源报告错误，便于排查是谁挂了。
    """
    errs = []
    for base in [""] + _mirrors():
        data, err = http_json(base + (RELEASE_API % repo))
        if data and data.get("tag_name"):
            return data["tag_name"].strip(), None
        errs.append("%s→%s" % (base or "直连", err or "无 tag_name"))
    return None, "; ".join(errs)


def normalize(tag):
    """去掉 v 前缀（v1.19.32 与 1.19.32 视为同一版本）。"""
    t = (tag or "").strip()
    return t[1:] if t[:1] in ("v", "V") else t


def load_state(path=STATE_FILE):
    """读上次打包记录。缺失/损坏时返回空状态（按首次运行处理）。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {}
        return d
    except (OSError, ValueError):
        return {}


def write_outputs(pairs, out_path=None):
    """把结果写成 key=value。CI 里追加到 $GITHUB_OUTPUT。"""
    lines = ["%s=%s" % (k, v) for k, v in pairs]
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if out_path:
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines) + "\n")
    if not gh_out and not out_path:
        for line in lines:
            print(line)


def main():
    ap = argparse.ArgumentParser(description="探测上游版本更新")
    ap.add_argument("--out", default="", help="把 key=value 结果也写到该文件")
    args = ap.parse_args()

    results, failures = {}, []
    for key, repo, label in TARGETS:
        tag, err = latest_tag(repo)
        if tag:
            results[key] = tag
        else:
            results[key] = None
            failures.append("%s（%s）：%s" % (label, repo, err))

    print("=" * 66)
    print("  上游版本探测")
    print("=" * 66)

    if failures:
        # 探测失败必须显式暴露：绝不能让 workflow 把「查不到」当成「没更新」
        print("")
        for f in failures:
            print("  [FAIL] " + f)
        print("")
        print("ERROR: 有上游版本探测失败，无法判断是否有更新。")
        print("       这**不**等同于「无更新」——请检查网络/限流后重跑。")
        return 2

    kernel = normalize(results["mihomo"])
    mcx = normalize(results["metacubexd"])
    zash = normalize(results["zashboard"])

    st = load_state()
    st_kernel = (st.get("kernel") or "").strip()
    st_mcx = (st.get("metacubexd") or "").strip()
    st_zash = (st.get("zashboard") or "").strip()
    try:
        st_seq = int(st.get("panel_seq") or 0)
    except (TypeError, ValueError):
        st_seq = 0

    print("  %-16s %-14s %s" % ("组件", "上游最新", "上次打包"))
    print("  " + "-" * 58)
    print("  %-16s %-14s %s" % ("mihomo 内核", results["mihomo"], st_kernel or "(未记录)"))
    print("  %-16s %-14s %s" % ("MetaCubeXD", results["metacubexd"], st_mcx or "(未记录)"))
    print("  %-16s %-14s %s" % ("Zashboard", results["zashboard"], st_zash or "(未记录)"))
    print("")

    # 首次运行（还没有任何记录）时，不把「记录为空」当成「面板有变化」，
    # 否则第一次跑会凭空多出一个 .1 版本。
    first_run = not st_kernel
    kernel_changed = (not first_run) and (st_kernel != kernel)
    if first_run:
        panels_changed = False
    else:
        panels_changed = (mcx != st_mcx) or (zash != st_zash)

    if first_run:
        print("  → 首次运行：尚无上次记录，按「以内核版本建基线」处理")
    elif kernel_changed:
        print("  → 内核 %s → %s（序号归零）" % (st_kernel, kernel))
    else:
        print("  → 内核未变：%s" % kernel)

    if not first_run and panels_changed:
        print("  → 面板有变化：MetaCubeXD %s→%s，Zashboard %s→%s"
              % (st_mcx or "(空)", mcx, st_zash or "(空)", zash))
    elif not first_run:
        print("  → 面板无变化")

    # 计算本次包版本
    #   首次运行 / 内核变了 → <内核版本>（序号归零）
    #   只有面板变        → <内核版本>.<seq+1>
    if first_run or kernel_changed:
        pkg_version = kernel
        next_seq = 0
    elif panels_changed:
        next_seq = st_seq + 1
        pkg_version = "%s.%d" % (kernel, next_seq)
    else:
        pkg_version = ""
        next_seq = st_seq

    needs_build = bool(first_run or kernel_changed or panels_changed)

    print("")
    print("  %-16s %s" % ("是否需要构建：", "是" if needs_build else "否"))
    if needs_build:
        print("  %-16s %s（tag v%s）" % ("本次包版本：", pkg_version, pkg_version))

    write_outputs([
        ("kernel", kernel),
        ("metacubexd", mcx),
        ("zashboard", zash),
        ("kernel_changed", "true" if kernel_changed else "false"),
        ("panels_changed", "true" if panels_changed else "false"),
        ("first_run", "true" if first_run else "false"),
        ("needs_build", "true" if needs_build else "false"),
        ("package_version", pkg_version),
        ("panel_seq", str(next_seq)),
        # Release tag 约定：v<包版本>（与 build-fpk.yml 的 v* 触发一致）
        ("release_tag", ("v" + pkg_version) if pkg_version else ""),
    ], args.out or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
