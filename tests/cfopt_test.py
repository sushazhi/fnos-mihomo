#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cfopt.py 黑盒回归测试套件（共享任务 task-1）。

被测对象：``app/cfopt.py`` —— 由 ``cmd/main`` 里内嵌的 3 段 heredoc 抽出的独立 CLI。
本套件**只**通过子进程黑盒调用（``sys.executable app/cfopt.py ...``，cwd=仓库根），
不 import 实现内部函数、不依赖内部文件布局，只认下面这份冻结契约。

冻结契约（逐条编码成断言）
--------------------------
- 调用方式::

      python3 app/cfopt.py fetch   <url> <ipfile>
      python3 app/cfopt.py measure <cidrs> <ipfile> <sni> <candidates> <conc> <timeout_ms> <speedtest> <history> <top>
      python3 app/cfopt.py inject  <cfg> <ipfile> <domains> <top> <snippet>

- 环境变量 ``CFOPT_MARK``（缺省 ``# cfopt:``）。
- 全部日志走 stdout/stderr；断言**关键字**，不断言整行。
- 退出码：fetch 0/1/2；measure 0/2；inject 0/1。
- fetch 的 IP 解析**IPv4 / IPv6 都收**（含 ``ip:port`` 与 ``[IPv6]:port``），只丢非公网地址。
- fetch/measure **只在成功时原子替换** ``<ipfile>``，失败时保持原样。
- inject **总是**写 ``<cfg>.cfopt-new``，成功注入时写 ``<snippet>``；
  功能关闭或本次放弃时可能删 ``<snippet>``（仅当 mtime 早于本次进程启动时刻）；
  **绝不改动 ``<cfg>`` 本身**。
- 配置里**没有 ``rules`` 键**时整次放弃（不得凭空补 ``MATCH,DIRECT``：那会把本该走代理的
  流量静默改成全直连）—— 见 C 组 ``test_C_no_rules_key_is_abandoned``。
- 写文件都是 LF；读入 universal newlines（CRLF 输入 → LF 输出）；LF 是**硬断言**。
  另外所有字节比较仍先归一化 CRLF（Windows 文本模式陷阱）。

套件结构（与契约 A/B/C/D/E 分组一一对应）
------------------------------------------
- A  16 个夹具的端到端契约（tests/fixtures/*.yaml，只读）
- B  注入成功的 4 条行为不变量（每例都断言）+ 片段文件的写/删/mtime 护栏 + 多 base 域名
- C  流式片段对拍（hosts 12 例 + rules 1 例）+ 缺 ``rules`` 键的三种形状
- D  PyYAML 有无两条路径产物逐字节一致（含「证明 stub 真的屏蔽了 PyYAML」的自证）
- E  失败回退（fetch 网络失败 / measure 全部不可达 时 ``<ipfile>`` 保持不变）
- F  额外：本地服务下的成功路径（fetch 纯 v4/纯 v6/混合/CSV/404、measure 成功）
- G  额外：CRLF 输入 → LF 输出，以及所有产物一律 LF（硬断言）

运行
----
::

    python3 tests/cfopt_test.py                # 正常跑，打印每例结果与总通过数
    python3 tests/cfopt_test.py --self-check   # 追加一条“故意写错的断言”，证明套件真的会红

    # unittest 发现（tests/test_cfopt.py 是转发 shim，让裸 discover 也能收齐）：
    python3 -m unittest discover -s tests            # 经 shim 收齐用例
    python3 -m unittest discover -s tests -p 'cfopt_test.py'   # 或直接指定文件名
    # 注意：cfopt_test.py 本身不匹配 discover 默认模式 test*.py，裸 discover 靠
    # tests/test_cfopt.py 这个一行 shim 转发；没有 shim 会得到 “Ran 0 tests”。

环境开关
--------
- ``CFOPT_TEST_SELFCHECK=1``（等价 ``--self-check``）：打开自检用例；默认跳过，不污染正常结果。
- ``CFOPT_TEST_STRICT_CONTRACT=1``：inject 的 ``argv[1]`` 必须是候选文件
  （lead 已裁决的口径）；配合 ``CFOPT_TEST_INJECT_ARGV=auto`` 时才有实测意义。
- ``CFOPT_TEST_INJECT_ARGV=candidate|literal|auto``：inject 的 ``argv[1]`` 语义。
  默认 ``candidate``（bash 先 ``cp 原配置 <cfg>.cfopt-new``，再把候选传给 python，
  实现绝不碰原配置）；``literal``/``auto`` 只用于排障或口径变更。
- ``CFOPT_TEST_BIN=<abs path>``：用替身实现自查套件本身（不写进仓库）。
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import yaml  # 测试自身用 PyYAML 校验产物；被测进程用不用由它自己决定
    HAVE_YAML = True
    YAML_ERR = ""
except Exception as exc:  # pragma: no cover - 仅环境缺 PyYAML 时
    yaml = None
    HAVE_YAML = False
    YAML_ERR = str(exc)

# Windows 控制台默认按本地代码页（如 cp936）编码 stdout，中文断言消息会变成乱码。
# 除非调用方显式设置了 PYTHONIOENCODING，这里统一按 UTF-8 输出（本仓库工具链按 UTF-8 读取）。
if not os.environ.get("PYTHONIOENCODING"):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ---------------------------------------------------------------- 常量 / 路径

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXDIR = os.path.join(ROOT, "tests", "fixtures")
NOYAML = os.path.join(ROOT, "tests", "_no_yaml")
DEFAULT_BIN = os.path.join(ROOT, "app", "cfopt.py")
BIN = os.environ.get("CFOPT_TEST_BIN") or DEFAULT_BIN

MARK = "# cfopt:"
KEY = "+.example.com"
OUR_RULE = "DOMAIN-SUFFIX,example.com,DIRECT"
DOMAINS = "example.com"
TOP = "4"
# 冻结契约指定的 IP 列表：104.16.0.1 + 2606:4700::1111 + 172.64.1.1
IPS = ["104.16.0.1", "2606:4700::1111", "172.64.1.1"]
IPFILE_BODY = "\n".join(IPS) + "\n"

SELFCHECK = os.environ.get("CFOPT_TEST_SELFCHECK") == "1"
STRICT_CONTRACT = os.environ.get("CFOPT_TEST_STRICT_CONTRACT") == "1"
# inject 的 argv[1] 语义：candidate = lead 裁决口径（bash 先 cp，再传候选）。
ARGV_MODE = os.environ.get("CFOPT_TEST_INJECT_ARGV") or "candidate"
if ARGV_MODE not in ("candidate", "literal", "auto"):
    ARGV_MODE = "candidate"

_HEADER_RE = re.compile(r"^[\"']?(?:hosts|rules)[\"']?\s*:")


# ---------------------------------------------------------------- 小工具

def norm_bytes(b):
    """归一化 CRLF（Windows 文本模式会把 \\n 写成 \\r\\n）。"""
    if b is None:
        return None
    return b.replace(b"\r\n", b"\n")


def norm_text(t):
    if t is None:
        return None
    return t.replace("\r\n", "\n")


def read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def write_bytes(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def is_header(ln):
    """顶层 hosts/rules 头行（可能带引号）。"""
    return bool(_HEADER_RE.match(ln))


def drop_mark_lines(text):
    """按行剔除含 CFOPT_MARK 的行（用于求「用户原始内容」基线）。"""
    return "\n".join(ln for ln in norm_text(text).split("\n") if MARK not in ln)


def byte_content_lines(text):
    """参与逐行计数比较的内容行：去掉标记行、顶层 hosts/rules 头行、空行，再 strip 缩进。

    为什么要 strip：流式 ``hosts: {...}`` 会被安全地转成块式并重新缩进，
    这是契约允许的形态变化；逐字节的原始比较只对非流式夹具可用（见 B4）。
    为什么要丢头行：`rules: []` → `rules:` 是契约允许的「null → 块式」改写。
    """
    out = []
    for ln in norm_text(text).split("\n"):
        if MARK in ln:
            continue
        if is_header(ln):
            continue
        if not ln.strip():
            continue
        out.append(ln.strip())
    return out


def yload(text):
    """safe_load；失败直接抛（断言里当作“YAML 不可解析”）。"""
    return yaml.safe_load(text)


def dup_keys(text):
    """返回输出里重复出现的 YAML 映射键（同名键必须合并，不得重复）。"""
    node = yaml.compose(text)
    found = []

    def walk(n):
        if isinstance(n, yaml.MappingNode):
            seen = set()
            for k, v in n.value:
                kk = getattr(k, "value", "<non-scalar>")
                kk = str(kk)
                if kk in seen:
                    found.append(kk)
                seen.add(kk)
                walk(v)
        elif isinstance(n, yaml.SequenceNode):
            for v in n.value:
                walk(v)

    if node is not None:
        walk(node)
    return found


def as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def canon(v):
    """值的规范形（hosts 的值可能是标量、列表、嵌套映射，统一成可比较结构）。"""
    if isinstance(v, dict):
        return {str(k): canon(x) for k, x in v.items()}
    if isinstance(v, list):
        return [canon(x) for x in v]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    return str(v)


def hosts_of(doc):
    h = doc.get("hosts") if isinstance(doc, dict) else None
    return h if isinstance(h, dict) else {}


def rules_of(doc):
    r = doc.get("rules") if isinstance(doc, dict) else None
    if r is None:
        return []
    if isinstance(r, list):
        return [str(x) for x in r]
    return [str(r)]


def is_subsequence(needle, hay):
    it = iter(hay)
    return all(any(x == n for x in it) for n in needle)


def strip_snippet_timestamp(data):
    """<snippet> 里带生成时间，逐字节对拍前必须先去掉那一行。"""
    if data is None:
        return None
    text = norm_bytes(data).decode("utf-8", "replace")
    return "\n".join(ln for ln in text.split("\n") if "生成时间" not in ln)


# ---------------------------------------------------------------- 告警收集

NOTES = []


def note(msg):
    if msg not in NOTES:
        NOTES.append(msg)


class HarnessUnavailable(Exception):
    pass


# ---------------------------------------------------------------- 黑盒执行器

class Harness:
    """统一的黑盒调用入口：临时目录、环境变量、inject argv 语义探测与结果缓存。"""

    def __init__(self, root):
        self.root = root
        self.tmp = tempfile.mkdtemp(prefix="cfopt-tests-")
        self._cache = {}
        self._modes = {}

    # ---------- 基础设施 ----------
    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fresh_dir(self, name, py_path):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", "%s" % name)
        d = os.path.join(self.tmp, "noyaml" if py_path == "noyaml" else "yaml", safe)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        return d

    def env_for(self, py_path):
        env = os.environ.copy()
        # Windows 上子进程 stdout 默认按本地代码页（cp936）编码，中文日志会乱码，
        # 断言关键字就不可靠；部署环境（fnOS/Linux）本来就是 UTF-8，这里统一按 UTF-8 取日志。
        env["PYTHONIOENCODING"] = "utf-8"
        if py_path == "noyaml":
            prev = env.get("PYTHONPATH") or ""
            env["PYTHONPATH"] = NOYAML + ((os.pathsep + prev) if prev else "")
        return env

    def run(self, args, py_path="yaml", timeout=120):
        cmd = [sys.executable, BIN] + [str(a) for a in args]
        return self._spawn(cmd, py_path, timeout)

    def run_python(self, code, py_path="yaml", timeout=60):
        """跑一段裸 python（用于验证 PYTHONPATH 屏蔽是否真的生效）。"""
        return self._spawn([sys.executable, "-c", code], py_path, timeout)

    def _spawn(self, cmd, py_path, timeout):
        proc = subprocess.run(
            cmd, cwd=self.root, env=self.env_for(py_path),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
        )
        text = proc.stdout.decode("utf-8", "replace")
        return proc.returncode, text

    # ---------- inject argv 语义 ----------
    def mode(self, py_path="yaml"):
        """inject 的 argv[1] 语义。

        契约口径（lead 已裁决，2026-10-18）：argv[1] 是**候选文件** —— bash 那层先
        ``cp 原配置 <cfg>.cfopt-new``，再把候选路径传给 python；python 读它、改它、
        写回它，**绝不碰原配置**。本套件默认就严格按这个口径跑
        （``CFOPT_TEST_INJECT_ARGV=candidate``），先 cp 出候选再传候选。

        - candidate : lead 裁决的口径（默认）
        - literal   : argv[1]=原配置、实现自己 cp（只保留为兼容开关）
        - auto      : 实测探测（排障用，可能把口径不一致掩盖过去）
        - unknown   : auto 下两者都没观察到
        """
        if ARGV_MODE in ("candidate", "literal"):
            return ARGV_MODE
        if py_path in self._modes:
            return self._modes[py_path]
        if not os.path.exists(BIN):
            self._modes[py_path] = "missing"
            return "missing"
        d = self._fresh_dir("__probe__", py_path)
        cfg = os.path.join(d, "probe.yaml")
        src = b"hosts: {a.example.com: 1.2.3.4}\nrules: []\n"
        write_bytes(cfg, src)
        ipfile = os.path.join(d, "ips.txt")
        write_bytes(ipfile, IPFILE_BODY.encode())
        snippet = os.path.join(d, "snip.txt")
        try:
            self.run(["inject", cfg, ipfile, DOMAINS, TOP, snippet], py_path)
        except Exception as exc:  # 探测本身失败
            self._modes[py_path] = "unknown"
            note("inject argv 语义探测异常：%s" % exc)
            return "unknown"
        cand = cfg + ".cfopt-new"
        if os.path.exists(cand):
            m = "literal"
        elif read_bytes(cfg) not in (None, src):
            m = "candidate"
        else:
            m = "unknown"
        self._modes[py_path] = m
        return m

    # ---------- inject 用例 ----------
    def inject_case(self, key, py_path="yaml", domains=DOMAINS, top=TOP,
                    fixture=None, text=None, raw=None, pre_snippet=False,
                    snippet_delta=None):
        """跑一次 inject，返回结果快照（按参数缓存，A/B/D 复用同一批子进程）。"""
        ck = (key, py_path, domains, top, fixture, text, raw, pre_snippet, snippet_delta)
        if ck in self._cache:
            return self._cache[ck]
        if raw is not None:
            src = raw
        elif fixture is not None:
            src = read_bytes(os.path.join(FIXDIR, fixture + ".yaml"))
            if src is None:
                raise HarnessUnavailable("夹具缺失：%s.yaml" % fixture)
        elif text is not None:
            src = text.encode("utf-8")
        else:
            raise ValueError("inject_case 必须给出 fixture / text / raw 之一（拒绝隐式空配置）")

        mode = self.mode(py_path)
        if mode in ("missing", "unknown"):
            raise HarnessUnavailable(
                "inject argv 语义 = %s（实现缺失或行为不符契约）" % mode)

        d = self._fresh_dir(key, py_path)
        cfg = os.path.join(d, "config.yaml")
        write_bytes(cfg, src)
        ipfile = os.path.join(d, "cfopt-ips.txt")
        write_bytes(ipfile, IPFILE_BODY.encode())
        snippet = os.path.join(d, "cfopt-hosts.txt")
        if pre_snippet:
            write_bytes(snippet, b"STALE-CFOPT-SNIPPET\n")
            t = time.time() + (snippet_delta if snippet_delta is not None else -3600.0)
            os.utime(snippet, (t, t))

        if mode == "candidate":
            arg = cfg + ".cfopt-new"
            write_bytes(arg, src)
        else:
            arg = cfg

        rc, log = self.run(["inject", arg, ipfile, domains, top, snippet], py_path)

        res = {
            "key": key, "mode": mode, "rc": rc, "log": log,
            "dir": d, "cfg": cfg, "new": cfg + ".cfopt-new", "snippet": snippet,
            "ipfile": ipfile, "src": src,
            "cfg_bytes": read_bytes(cfg),
            "new_bytes": read_bytes(cfg + ".cfopt-new"),
            "snippet_bytes": read_bytes(snippet),
            "pre_snippet": pre_snippet,
        }
        if mode == "candidate" and os.path.exists(cfg + ".cfopt-new.cfopt-new"):
            note("WARN: 疑似实现按 literal 口径工作（argv[1]=原配置）——"
                 "请用 CFOPT_TEST_INJECT_ARGV=literal 复跑，或请 lead 复核口径")
        if res["new_bytes"] is not None and b"\r\n" in res["new_bytes"]:
            note("WARN: <cfg>.cfopt-new 含 CRLF（契约要求 LF）")
        if res["snippet_bytes"] is not None and b"\r\n" in res["snippet_bytes"]:
            note("WARN: <snippet> 含 CRLF（契约要求 LF）")
        self._cache[ck] = res
        return res


HARNESS = Harness(ROOT)


# ---------------------------------------------------------------- 夹具期望表

CASES = {
    # kind: ok=注入成功 / abort=写法不支持整次放弃 / disabled=功能未启用应清除
    # flow: hosts 或 rules 用了流式写法（此时形态会转块式，B4 的逐行比较不适用）
    "f8":    {"kind": "ok", "flow": False, "hosts_keys": ["keep.example.net"], "user_rules": []},
    "f1":    {"kind": "ok", "flow": True, "hosts_keys": ["a.example.com"], "user_rules": []},
    "f4":    {"kind": "ok", "flow": True, "hosts_keys": [], "user_rules": []},
    "f10":   {"kind": "ok", "flow": False, "hosts_keys": ["a.example.com"],
              "user_rules": ["DOMAIN-SUFFIX,x.com,PROXY"]},
    "f05":   {"kind": "ok", "flow": False, "hosts_keys": [],
              "user_rules": ["DOMAIN-SUFFIX,example.net,PROXY"]},
    "L1":    {"kind": "abort", "flow": False, "reason": "hosts", "snippet": "old"},
    "L2":    {"kind": "ok", "flow": True, "hosts_keys": ["a.example.com"], "user_rules": []},
    "M1":    {"kind": "ok", "flow": True, "hosts_keys": [],
              "user_rules": ["DOMAIN-SUFFIX,example.net,PROXY", "MATCH,DIRECT"]},
    "M1b":   {"kind": "abort", "flow": False, "reason": "rules", "snippet": "old"},
    "M2off": {"kind": "disabled", "flow": False, "domains": "", "snippet": "old"},
    "M3":    {"kind": "ok", "flow": True, "hosts_keys": [], "user_rules": []},
    "N1":    {"kind": "ok", "flow": False, "hosts_keys": [],
              "user_rules": ["MATCH,PROXY"], "log": "inject_only"},
    "N3a":   {"kind": "abort", "flow": False, "reason": "rules"},
    "N3b":   {"kind": "abort", "flow": False, "reason": "hosts"},
}
OK_NAMES = [n for n, s in CASES.items() if s["kind"] == "ok"]
ABORT_NAMES = [n for n, s in CASES.items() if s["kind"] == "abort"]

# 契约 C：流式 hosts 片段（12 例）
FLOW_HOSTS = [
    "{a: 1.2.3.4, b: [1.2.3.4, 5.6.7.8]}",
    '{a: "1.2.3.4,5.6.7.8"}',
    "{a: 'x, y', b: 2}",
    "{\"'\": 2}",
    "{a: 'it''s, ok', b: 2}",
    '{a: "a\\"b, c", b: 2}',
    "{a: {x: 1, y: 2}, b: 3}",
    "{'a,b': 1, c: 2}",
    "{'+.example.com': 4.4.4.4}",
    "{a: 1, 'b,c': {d: [1,2], e: ' x , y '}, f: g}",
    "{a: '', b: 2}",
    "{a: 1, b: 2, c: 3}",
]
# 契约 C 的 +1：流式 rules
FLOW_RULES_CFG = "hosts: {}\nrules: ['DOMAIN-SUFFIX,a.com,PROXY']\n"


def flow_hosts_cfg(piece):
    return "hosts: %s\nrules: []\n" % piece


# ---------------------------------------------------------------- 基类

class Base(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.h = HARNESS
        self.gate()

    def gate(self):
        if not os.path.exists(BIN):
            self.fail("[precheck] 待测实现不存在：%s（契约驱动，待 lead 冻结后联调）" % BIN)
        m = self.h.mode("yaml")
        if m == "unknown":
            self.fail("[precheck] inject 的 argv[1] 语义不符契约口径：既没写候选，也没写原配置"
                      "（lead 裁决：argv[1] = <cfg>.cfopt-new 候选）")
        if STRICT_CONTRACT and m != "candidate":
            self.fail("[precheck] CFOPT_TEST_STRICT_CONTRACT=1：inject 的 argv[1] 必须是候选文件"
                      "（lead 已裁决），实测=%r" % m)

    def run_inject(self, name, py_path="yaml"):
        spec = CASES[name]
        domains = spec.get("domains", DOMAINS)
        pre = spec.get("snippet") == "old"
        return self.h.inject_case(name, py_path=py_path, domains=domains,
                                  pre_snippet=pre, snippet_delta=(-3600.0 if pre else None),
                                  fixture=name)

    # ---------- 通用断言 ----------
    def assert_yaml_ok(self, name, out):
        doc = yload(out)  # 解析失败即抛 TestCase 失败
        self.assertIsInstance(doc, dict, "%s: 输出顶层应是映射" % name)
        self.assertEqual(dup_keys(out), [], "%s: 输出不得有重复 YAML 键（同名键必须合并）" % name)
        return doc

    def assert_no_unrelated_edits(self, name, base_doc, out_doc):
        for k, v in (base_doc or {}).items():
            if k in ("hosts", "rules"):
                continue
            self.assertIn(k, out_doc, "%s: 顶层键 %r 被删除" % (name, k))
            self.assertEqual(out_doc[k], v, "%s: 顶层键 %r 的内容被无关改动" % (name, k))
        for k in (out_doc or {}):
            if k in ("hosts", "rules"):
                continue
            self.assertIn(k, base_doc or {}, "%s: 输出现新增了顶层键 %r" % (name, k))
        bh, oh = hosts_of(base_doc), hosts_of(out_doc)
        for k, v in bh.items():
            if k == KEY:
                continue  # 同名键按契约合并，允许被本轮注入覆盖
            self.assertIn(k, oh, "%s: 用户 hosts 键 %r 被丢弃" % (name, k))
            self.assertEqual(canon(oh[k]), canon(v), "%s: 用户 hosts 键 %r 的值被改动" % (name, k))
        for k in oh:
            if k == KEY:
                continue
            self.assertIn(k, bh, "%s: hosts 新增了非注入键 %r" % (name, k))

    def assert_success_invariants(self, name, spec, r, doc, out, src):
        base_doc = yload(drop_mark_lines(src))
        out_rules = rules_of(doc)
        base_rules = rules_of(base_doc)
        # 自证：期望表里的 user_rules 与基线解析一致
        self.assertEqual(base_rules, spec["user_rules"],
                         "%s: 夹具基线规则与期望表不符（测试表自身错误）" % name)
        # B1：我们的 DIRECT 恰好 1 条，且是第 0 项
        self.assertEqual(out_rules.count(OUR_RULE), 1,
                         "%s: 我们的 DIRECT 规则必须恰好 1 条" % name)
        self.assertTrue(out_rules and out_rules[0] == OUR_RULE,
                        "%s: 我们的 DIRECT 规则必须是 rules 第 0 项，实际=%r" % (name, out_rules[:1]))
        # B2：用户原有规则全部保留、相对顺序不变，且不得凭空追加
        self.assertEqual(out_rules[1:], base_rules,
                         "%s: 用户原有规则必须按原顺序完整保留、不得凭空增删" % name)
        # B3：hosts[+.example.com] 的值包含我们给的 IP
        vals = as_list(hosts_of(doc).get(KEY))
        for ip in IPS:
            self.assertIn(ip, vals, "%s: hosts['%s'] 的值必须包含注入 IP %s（实际 %r）"
                          % (name, KEY, ip, vals))
        # 期望表里的用户 hosts 键一个不少
        for k in spec.get("hosts_keys", []):
            self.assertIn(k, hosts_of(doc), "%s: 用户 hosts 键 %r 丢失" % (name, k))
        # B4 解析级：未被触碰的顶层键深等 + 用户 hosts 条目保留
        self.assert_no_unrelated_edits(name, base_doc, doc)
        # B4 逐行级：非流式夹具（流式转块式会重排，改用上面的解析级检查）
        if not spec.get("flow"):
            self.assertEqual(Counter(byte_content_lines(out)), Counter(byte_content_lines(src)),
                             "%s: 除旧标记行与本次新写行外，输出与原输入应逐行一致" % name)

    # ---------- 三种契约分支 ----------
    def check_ok(self, name, spec, r):
        self.assertEqual(r["rc"], 0, "%s: inject 成功路径退出码应为 0" % name)
        log = r["log"]
        self.assertIn("注入", log, "%s: 成功注入日志应含『注入』关键字" % name)
        self.assertNotIn("写法不支持", log, "%s: 不应落入『写法不支持』分支" % name)
        if spec.get("log") == "inject_only":
            self.assertNotIn("功能未启用", log,
                             "%s: 不得出现假『功能未启用』日志（已修过的回归点）" % name)
        self.assertIsNotNone(r["new_bytes"], "%s: 必须总是写 <cfg>.cfopt-new" % name)
        out = norm_text(r["new_bytes"].decode("utf-8", "replace"))
        src = norm_text(r["src"].decode("utf-8", "replace"))
        doc = self.assert_yaml_ok(name, out)
        self.assert_success_invariants(name, spec, r, doc, out, src)
        # 片段文件：成功注入必须写，且含每个 IP 与 base 域名
        self.assertIsNotNone(r["snippet_bytes"], "%s: 成功注入必须写 <snippet>" % name)
        snip = norm_text(r["snippet_bytes"].decode("utf-8", "replace"))
        for ip in IPS:
            self.assertIn(ip, snip, "%s: <snippet> 缺少 IP %s" % (name, ip))
        self.assertIn("example.com", snip, "%s: <snippet> 缺少 base 域名" % name)

    def check_abort(self, name, spec, r):
        self.assertEqual(r["rc"], 0, "%s: 整次放弃也应退出码 0" % name)
        log = r["log"]
        self.assertIn("写法不支持", log, "%s: 日志应说明『写法不支持』" % name)
        if spec.get("reason"):
            self.assertIn("%s 写法不支持" % spec["reason"], log,
                          "%s: 应指名 %s 写法不支持" % (name, spec["reason"]))
        self.assertNotIn("条 DIRECT 规则", log, "%s: 整次放弃不得打印注入成功日志" % name)
        self.assertIsNotNone(r["new_bytes"], "%s: 必须总是写 <cfg>.cfopt-new" % name)
        out = r["new_bytes"].decode("utf-8", "replace")
        self.assertEqual(norm_bytes(r["new_bytes"]), norm_bytes(r["src"]),
                         "%s: 整次放弃时 <cfg>.cfopt-new 应与原文件逐字节一致" % name)
        self.assertNotIn(MARK, out, "%s: 整次放弃后不得残留任何注入标记" % name)
        self.assertNotIn(OUR_RULE, out, "%s: 整次放弃时不得写入我们的规则" % name)
        if spec.get("snippet") == "old":
            self.assertIsNone(r["snippet_bytes"], "%s: 整次放弃时陈旧 <snippet> 应被删除" % name)

    def check_disabled(self, name, spec, r):
        self.assertEqual(r["rc"], 0, "%s: 功能未启用也应退出码 0" % name)
        self.assertIn("功能未启用，已清除上一次的注入", r["log"],
                      "%s: 日志应含『功能未启用，已清除上一次的注入』" % name)
        self.assertIsNotNone(r["new_bytes"], "%s: 必须总是写 <cfg>.cfopt-new" % name)
        out = norm_text(r["new_bytes"].decode("utf-8", "replace"))
        src = norm_text(r["src"].decode("utf-8", "replace"))
        self.assertNotIn(MARK, out, "%s: 功能关闭后所有标记行都应被清除" % name)
        self.assertNotIn("104.16.0.1", out, "%s: 功能关闭后旧注入的 IP 应被清除" % name)
        self.assertEqual(norm_bytes(r["new_bytes"]), norm_bytes(drop_mark_lines(src).encode("utf-8")),
                         "%s: 关闭分支只应剔除标记行，其余内容逐字节保留" % name)
        if spec.get("snippet") == "old":
            self.assertIsNone(r["snippet_bytes"], "%s: 功能关闭时陈旧 <snippet> 应被删除" % name)


# ---------------------------------------------------------------- A 组：16 夹具

class AFixtures(Base):
    """A. 16 个夹具的端到端契约（YAML 可解析 + 我们的 DIRECT 规则位置 + 用户内容保留）。"""

    def _run_A(self, name):
        spec = CASES[name]
        r = self.run_inject(name)
        self.assertIsNotNone(r["new_bytes"], "%s: 必须总是写 <cfg>.cfopt-new" % name)
        # 契约核心：argv[1] 是候选，原配置绝不能被碰
        self.assertEqual(r["cfg_bytes"], r["src"], "%s: 绝不改动原配置本身" % name)
        if spec["kind"] == "ok":
            self.check_ok(name, spec, r)
            self._extras_ok(name, r)
        elif spec["kind"] == "abort":
            self.check_abort(name, spec, r)
        else:
            self.check_disabled(name, spec, r)

    def _extras_ok(self, name, r):
        out = norm_text(r["new_bytes"].decode("utf-8", "replace"))
        doc = yload(out)
        h = hosts_of(doc)
        if name == "f8":
            self.assertEqual(as_list(h.get("keep.example.net")), ["10.0.0.1"],
                             "f8: 缩进4的 hosts 块下夹注释时，用户键 keep.example.net 必须保留原值")
        elif name == "f1":
            self.assertEqual(h.get("a.example.com"), "1.2.3.4,5.6.7.8",
                             "f1: 用户引号内的逗号不得把值切坏")
        elif name == "f4":
            self.assertEqual(set(h.keys()), {KEY},
                             "f4: 用户已写 +.example.com 时必须合并为唯一一条，键集只应有它")
        elif name == "f10":
            self.assertEqual(str(h.get("a.example.com")), "1.2.1.1",
                             "f10: 带引号的顶层键下用户条目必须保留")
            self.assertIn("DOMAIN-SUFFIX,x.com,PROXY", rules_of(doc),
                          "f10: 用户的 DOMAIN-SUFFIX,x.com,PROXY 必须保留")
        elif name == "f05":
            lines = out.split("\n")
            self.assertIn("- 'DOMAIN-SUFFIX,example.net,PROXY'", lines,
                          "f05: 用户那条顶格序列项必须保留且缩进仍为 0")
        elif name == "L2":
            self.assertEqual(str(h.get("a.example.com")), "1.2.3.4",
                             "L2: hosts 行尾注释必须被剥离而不是否决整次注入")
        elif name == "M1":
            self.assertEqual(rules_of(doc),
                             [OUR_RULE, "DOMAIN-SUFFIX,example.net,PROXY", "MATCH,DIRECT"],
                             "M1: 规则必须恰好是 [我们的 DIRECT, 用户的 example.net, 用户的 MATCH,DIRECT]")
        elif name == "M3":
            self.assertEqual(str(h.get("a.example.com")), "1.2.3.4",
                             "M3: 紧贴的 #c 行尾注释不得否决本次注入")
        elif name == "N1":
            self.assertNotIn("1.1.1.1", out, "N1: 旧标记行里的旧 IP 必须被清除后再写新值")
            self.assertIn("注入", r["log"], "N1: 应打印注入成功日志")
            self.assertNotIn("功能未启用", r["log"],
                             "N1: 功能开启时不得出现假『功能未启用』日志（已修过的回归点）")


def _make_a_test(name):
    def t(self):
        self._run_A(name)
    t.__name__ = "test_A_" + name
    t.__doc__ = "夹具 %s 的端到端契约" % name
    return t


for _n in CASES:
    setattr(AFixtures, "test_A_" + _n, _make_a_test(_n))


class AFixtureSet(unittest.TestCase):
    """A 组元检查：夹具目录与期望表一致（不依赖被测实现）。"""

    def test_A_all_fixtures_count(self):
        """A 表必须正好覆盖 14 个夹具。"""
        self.assertEqual(len(CASES), 14, "A: 夹具期望表必须正好 14 例")
        have = sorted(f[:-5] for f in os.listdir(FIXDIR) if f.endswith(".yaml"))
        self.assertEqual(have, sorted(CASES), "A: tests/fixtures 下的夹具与期望表不一致")


# ---------------------------------------------------------------- B 组：不变量

class BInvariants(Base):
    """B. 注入成功的 4 条行为不变量（逐例断言）+ 片段文件护栏。"""

    def _iter_ok(self):
        for name in OK_NAMES:
            self.gate()
            yield name, CASES[name], self.run_inject(name)

    def test_B1_rule_exactly_one_and_first(self):
        """B.1 契约口径（lead 裁决 2026-10-19）：**每个 base 域名**恰好 1 条我们的 DIRECT
        规则，且这些规则整体位于 rules 最前面。本语料只有 example.com 一个 base，
        所以「恰好 1 条」；多 base 的情形见 test_B8。"""
        for name, spec, r in self._iter_ok():
            doc = yload(norm_text(r["new_bytes"].decode("utf-8", "replace")))
            rules = rules_of(doc)
            self.assertEqual(rules.count(OUR_RULE), 1,
                             "%s: 每个 base 域名恰好 1 条我们的 DIRECT（example.com → 1 条）" % name)
            self.assertTrue(rules and rules[0] == OUR_RULE,
                            "%s: 我们的 DIRECT 必须是 rules 第 0 项" % name)

    def test_B2_user_rules_preserved_in_order(self):
        for name, spec, r in self._iter_ok():
            out = norm_text(r["new_bytes"].decode("utf-8", "replace"))
            src = norm_text(r["src"].decode("utf-8", "replace"))
            base_rules = rules_of(yload(drop_mark_lines(src)))
            out_rules = rules_of(yload(out))
            self.assertEqual(out_rules[1:], base_rules,
                             "%s: 用户原有规则必须全部保留且相对顺序不变" % name)
            self.assertTrue(is_subsequence(base_rules, out_rules),
                            "%s: 用户规则应是输出的子序列" % name)

    def test_B3_our_host_key_contains_ips(self):
        for name, spec, r in self._iter_ok():
            doc = yload(norm_text(r["new_bytes"].decode("utf-8", "replace")))
            vals = as_list(hosts_of(doc).get(KEY))
            for ip in IPS:
                self.assertIn(ip, vals, "%s: hosts['%s'] 值应含 %s" % (name, KEY, ip))

    def test_B4_no_unrelated_edits(self):
        for name, spec, r in self._iter_ok():
            out = norm_text(r["new_bytes"].decode("utf-8", "replace"))
            src = norm_text(r["src"].decode("utf-8", "replace"))
            doc = yload(out)
            base_doc = yload(drop_mark_lines(src))
            self.assert_no_unrelated_edits(name, base_doc, doc)
            if not spec.get("flow"):
                self.assertEqual(Counter(byte_content_lines(out)),
                                 Counter(byte_content_lines(src)),
                                 "%s: 除标记行与新写行外应逐行一致" % name)

    def test_B8_multi_base_one_rule_each(self):
        """B.1 的真实语义：多 base 域名时，**每个 base** 各一条 DIRECT，整组置顶。
        用合成配置避免污染夹具表。"""
        text = ("hosts:\n"
                "  keep.example.net: 9.9.9.9\n"
                "rules:\n"
                "  - 'DOMAIN-SUFFIX,keep.example.net,PROXY'\n"
                "  - 'MATCH,PROXY'\n")
        r = self.h.inject_case("multi-base", text=text, domains="example.com,foo.net")
        self.assertIsNotNone(r["new_bytes"], "B8: 必须写 <cfg>.cfopt-new")
        out = norm_text(r["new_bytes"].decode("utf-8", "replace"))
        rules = rules_of(yload(out))
        ours = ["DOMAIN-SUFFIX,example.com,DIRECT", "DOMAIN-SUFFIX,foo.net,DIRECT"]
        self.assertEqual(rules[:2], ours,
                         "B8: 两个 base 各一条 DIRECT，且都在最前面（实际 %r）" % rules)
        self.assertEqual(rules[2:], ["DOMAIN-SUFFIX,keep.example.net,PROXY", "MATCH,PROXY"],
                         "B8: 用户规则原样保留在后面")
        hk = hosts_of(yload(out))
        for k in ("+.example.com", "+.foo.net"):
            self.assertIn(k, hk, "B8: 每个 base 都要有 hosts 注入键（缺 %s）" % k)
        self.assertIn("keep.example.net", hk, "B8: 用户自己的 hosts 键不能被删")

    def test_B5_snippet_written_on_success(self):
        for name, spec, r in self._iter_ok():
            self.assertIsNotNone(r["snippet_bytes"], "%s: 成功注入必须写 <snippet>" % name)

    def test_B6_snippet_deleted_when_abandoned(self):
        r = self.h.inject_case("M1b", pre_snippet=True, snippet_delta=-3600.0, fixture="M1b")
        self.assertIn("写法不支持", r["log"], "B6: M1b 应整次放弃")
        self.assertIsNone(r["snippet_bytes"], "B6: 本次放弃且片段 mtime 更早时，应删除陈旧 <snippet>")

    def test_B7_snippet_kept_when_mtime_not_older(self):
        r = self.h.inject_case("L1-future", pre_snippet=True, snippet_delta=+3600.0, fixture="L1")
        self.assertIsNotNone(r["snippet_bytes"],
                             "B7: 片段 mtime 不早于本次进程启动时刻时，绝不能删除（并发护栏）")
        self.assertEqual(r["snippet_bytes"], b"STALE-CFOPT-SNIPPET\n",
                         "B7: 未删除的片段内容应保持原样")


# ---------------------------------------------------------------- C 组：流式对拍

class CFlowPieces(Base):
    """C. 流式片段对拍：切分必须引号/嵌套感知，非同名键一个不少。"""

    def _check_piece(self, label, piece, cfg):
        self.gate()
        r = self.h.inject_case(label, text=cfg)
        self.assertEqual(r["rc"], 0, "%s: inject 退出码应为 0" % label)
        self.assertIn("注入", r["log"], "%s: 应注入成功（流式切分不得因引号/嵌套而整次放弃）" % label)
        out = norm_text(r["new_bytes"].decode("utf-8", "replace"))
        doc = self.assert_yaml_ok(label, out)
        expected = yload("hosts: %s\n" % piece)["hosts"] or {}
        oh = hosts_of(doc)
        self.assertIn(KEY, oh, "%s: 必须写出我们的注入键" % label)
        for ip in IPS:
            self.assertIn(ip, as_list(oh.get(KEY)), "%s: hosts[%s] 应含 %s" % (label, KEY, ip))
        # 非同名键一个不少、值不变
        for k, v in expected.items():
            if k == KEY:
                continue
            self.assertIn(k, oh, "%s: 流式片段键 %r 丢失" % (label, k))
            self.assertEqual(canon(oh[k]), canon(v), "%s: 流式片段键 %r 的值被切坏" % (label, k))
        # 输出里除注入键外不得新增键
        extra = sorted(k for k in oh if k != KEY and k not in expected)
        self.assertEqual(extra, [], "%s: 输出了片段里没有的 hosts 键 %r" % (label, extra))
        # 同名键必须合并：若片段里已有 +.example.com，最终只能有一个该键
        self.assertEqual(dup_keys(out), [], "%s: 输出不得有重复键" % label)
        self.assertEqual(rules_of(doc)[0], OUR_RULE, "%s: 规则应置顶" % label)

    def test_C_flow_hosts_pieces(self):
        for i, piece in enumerate(FLOW_HOSTS):
            # 把片段原文带进用例标签：失败信息里能直接看到是哪一段 YAML
            self._check_piece("flow-hosts-%02d hosts: %s" % (i, piece), piece, flow_hosts_cfg(piece))

    def test_C_flow_rules_piece(self):
        label = "flow-rules"
        self.gate()
        r = self.h.inject_case(label, text=FLOW_RULES_CFG)
        self.assertEqual(r["rc"], 0, "%s: inject 退出码应为 0" % label)
        out = norm_text(r["new_bytes"].decode("utf-8", "replace"))
        doc = self.assert_yaml_ok(label, out)
        self.assertEqual(rules_of(doc), [OUR_RULE, "DOMAIN-SUFFIX,a.com,PROXY"],
                         "C: 流式 rules 转块式后应把我们的规则插到最前，用户规则原样保留")


    def test_C_no_rules_key_is_abandoned(self):
        """配置里完全没有 `rules` 键 → 整次放弃（不得凭空补 MATCH,DIRECT）。

        lead 裁决（2026-10-19）：本应用生成/订阅写入的配置一定有 rules 键，缺了只可能是
        手写配置；凭空补一条 `MATCH,DIRECT` 会把本该走代理的流量静默改成**全直连**，
        比不注入更糟。因此三种形状都必须「候选逐字节不变 + 产物里不得出现 MATCH」。
        """
        shapes = [
            ("norules-block", "hosts:\n  keep.example.net: 9.9.9.9\nproxies: []\n"),
            ("norules-nohosts", "proxies:\n  - {name: a, type: trojan}\n"),
            ("norules-flowhosts", "hosts: {keep.example.net: 9.9.9.9}\nproxies: []\n"),
        ]
        self.gate()
        for label, cfg in shapes:
            with self.subTest(shape=label):
                r = self.h.inject_case(label, text=cfg)
                self.assertEqual(r["rc"], 0, "%s: 整次放弃是正常完成，退出码应为 0" % label)
                self.assertEqual(norm_bytes(r["new_bytes"]), norm_bytes(r["src"]),
                                 "%s: 放弃时候选必须与原文逐字节一致" % label)
                self.assertEqual(r["cfg_bytes"], r["src"], "%s: 绝不改动原配置" % label)
                self.assertIn("没有 rules 键", r["log"],
                              "%s: 必须打印「没有 rules 键」的专门日志" % label)
                self.assertIn("MATCH,DIRECT", r["log"],
                              "%s: 日志要说清为何不补 MATCH,DIRECT" % label)
                out = norm_text(r["new_bytes"].decode("utf-8", "replace"))
                self.assertNotIn("MATCH", out,
                                 "%s: 产物里绝不能出现凭空的 MATCH 规则" % label)
                self.assertNotIn(MARK, out, "%s: 放弃时不得留下任何注入标记行" % label)
                # 有库 / 无库两条路径必须一致
                r2 = self.h.inject_case(label, py_path="noyaml", text=cfg)
                self.assertEqual(norm_bytes(r2["new_bytes"]), norm_bytes(r["new_bytes"]),
                                 "%s: 放弃判定不得依赖 PyYAML（有库/无库产物应一致）" % label)


# ---------------------------------------------------------------- D 组：双路径

D_SPECS = []
for _n in CASES:
    D_SPECS.append({"key": _n, "fixture": _n, "domains": CASES[_n].get("domains", DOMAINS)})
for _i, _piece in enumerate(FLOW_HOSTS):
    D_SPECS.append({"key": "flow-hosts-%02d" % _i, "text": flow_hosts_cfg(_piece)})
D_SPECS.append({"key": "flow-rules", "text": FLOW_RULES_CFG})


class DTwoPaths(Base):
    """D. PyYAML 有无两条路径：产物必须逐字节一致（库只是更准的校验/切分，不是行为开关）。"""

    def test_D_stub_really_disables_pyyaml(self):
        """自证：tests/_no_yaml 必须真的让子进程 import yaml 失败，否则 D 组是空跑。"""
        rc, out = self.h.run_python("import yaml", "noyaml")
        self.assertNotEqual(rc, 0, "D: 屏蔽 PyYAML 后 import yaml 应失败")
        self.assertIn("PyYAML disabled for this test", out,
                      "D: 失败的必须是 _no_yaml/yaml.py 这个 stub，而不是宿主根本没有 PyYAML"
                      "（否则 D 组并非在验证回退路径）")
        rc2, out2 = self.h.run_python(
            "import yaml, sys; sys.stdout.write('PYYAML' + '_VERSION=' + getattr(yaml, '__version__', '?'))",
            "yaml")
        if "PYYAML_VERSION" not in out2:
            note("WARN: 宿主环境未安装 PyYAML —— 正常路径退化为回退路径，D 组一致性意义有限")
        else:
            self.assertEqual(rc2, 0, "D: 正常路径 import yaml 应成功")

    def test_D_both_paths_produce_identical_bytes(self):
        self.gate()
        m_no = self.h.mode("noyaml")
        self.assertIn(m_no, ("literal", "candidate"),
                      "D: 屏蔽 PyYAML 后 inject 无法产出（argv 语义=%s）——实现未正确回退" % m_no)
        for spec in D_SPECS:
            kw = {k: v for k, v in spec.items() if k != "key"}
            a = self.h.inject_case(py_path="yaml", **spec)
            b = self.h.inject_case(py_path="noyaml", **spec)
            self.assertEqual(a["rc"], b["rc"], "%s: 两条路径退出码必须一致" % spec["key"])
            self.assertEqual(norm_bytes(a["new_bytes"]), norm_bytes(b["new_bytes"]),
                             "%s: PyYAML 有/无两条路径的 <cfg>.cfopt-new 必须逐字节一致" % spec["key"])
            self.assertEqual(strip_snippet_timestamp(a["snippet_bytes"]),
                             strip_snippet_timestamp(b["snippet_bytes"]),
                             "%s: 两条路径的 <snippet> 必须一致（时间戳行除外）" % spec["key"])
            self.assertEqual(MARK.encode() in (a["new_bytes"] or b""),
                             MARK.encode() in (b["new_bytes"] or b""),
                             "%s: 两条路径的注入标记状态必须一致" % spec["key"])


# ---------------------------------------------------------------- E 组：失败回退

class EFallbacks(Base):
    """E. 失败回退：fetch/measure 失败时 <ipfile> 必须逐字节保持原样。"""

    def _fresh(self):
        d = tempfile.mkdtemp(prefix="cfopt-e-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def test_E_fetch_network_failure_keeps_ipfile(self):
        d = self._fresh()
        ipfile = os.path.join(d, "cfopt-ips.txt")
        before = b"9.9.9.9\n"
        write_bytes(ipfile, before)
        rc, log = self.h.run(["fetch", "http://127.0.0.1:1/cfopt-no-such-url", ipfile], "yaml")
        self.assertNotEqual(rc, 0, "E: fetch 打不存在的 URL 必须非 0 退出（日志：%s）" % log.strip())
        self.assertEqual(read_bytes(ipfile), before, "E: fetch 失败时 <ipfile> 必须逐字节保持原样")

    def test_E_measure_unreachable_keeps_ipfile(self):
        d = self._fresh()
        ipfile = os.path.join(d, "cfopt-ips.txt")
        history = os.path.join(d, "cfopt-history.log")
        before = b"9.9.9.9\n"
        write_bytes(ipfile, before)
        rc, log = self.h.run(["measure", "192.0.2.0/30", ipfile, "example.com",
                              "2", "4", "300", "false", history, "4"], "yaml")
        self.assertEqual(rc, 2, "E: measure 全部候选不可达必须退出码 2（日志：%s）" % log.strip())
        self.assertEqual(read_bytes(ipfile), before, "E: measure 失败时 <ipfile> 必须逐字节保持原样")


# ---------------------------------------------------------------- F 组：本地 fetch 成功

class _BlobHandler(BaseHTTPRequestHandler):
    body = b""
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # 静音
        pass


class FFetchLocal(Base):
    """F. 额外：用本地 HTTP 服务验证 fetch 成功路径（不依赖外网）。"""

    def _serve(self, body, status=200):
        httpd = HTTPServer(("127.0.0.1", 0), _BlobHandler)
        httpd.RequestHandlerClass.body = body
        httpd.RequestHandlerClass.status = status
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return "http://127.0.0.1:%d/blob" % httpd.server_address[1]

    def _fetch_and_check(self, body, expect_ips):
        if isinstance(expect_ips, str):
            expect_ips = [expect_ips]
        d = tempfile.mkdtemp(prefix="cfopt-f-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ipfile = os.path.join(d, "cfopt-ips.txt")
        write_bytes(ipfile, b"9.9.9.9\n")
        url = self._serve(body)
        rc, log = self.h.run(["fetch", url, ipfile], "yaml")
        self.assertEqual(rc, 0, "F: fetch 成功路径应退出码 0（日志：%s）" % log.strip())
        raw = read_bytes(ipfile)
        text = norm_bytes(raw).decode("utf-8", "replace")
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        self.assertEqual(lines, expect_ips,
                         "F: <ipfile> 应恰好写入 %r（实际 %r）" % (expect_ips, lines))
        for ln in lines:
            try:
                ipaddress.ip_address(ln)
            except ValueError:
                self.fail("F: <ipfile> 出现非 IP 行 %r（应只含纯 IP 行）" % ln)
        self.assertTrue(text.endswith("\n"), "F: <ipfile> 应以换行结尾")
        self.assertNotIn(b"\r", raw, "F: 写文件一律 LF（契约），<ipfile> 不得含 \\r")

    def test_F_fetch_plain_ip_list(self):
        self._fetch_and_check(b"104.16.0.1\n", "104.16.0.1")

    def test_F_fetch_speedtest_csv(self):
        body = (b"IP,\xe5\x9c\xb0\xe5\x9d\x80,\xe7\xab\xaf\xe5\x8f\xa3,\xe5\xb9\xb3\xe5\x9d\x87\xe5\xbb\xb6\xe8\xbf\x9f,\xe4\xb8\x8b\xe8\xbd\xbd\xe9\x80\x9f\xe5\xba\xa6\n"
                b"104.16.0.1:443,104.16.0.1,443,12.5,88.8\n"
                b"172.64.1.1:443,172.64.1.1,443,20.0,50.0\n")
        self._fetch_and_check(body, ["104.16.0.1", "172.64.1.1"])

    def test_F_fetch_ipv6_only(self):
        """lead 实测反驳后的正向用例：纯全局 IPv6 响应 → exit 0，ipfile 收到 IPv6。"""
        self._fetch_and_check(b"2606:4700::1111\n2606:4700::1001\n",
                              ["2606:4700::1111", "2606:4700::1001"])

    def test_F_fetch_mixed_and_bracketed_v6(self):
        """IPv4 / IPv6 / `[IPv6]:port` 三种写法都要收；非公网地址（保留段）要丢。"""
        body = (b"104.16.0.1\n"
                b"[2606:4700::1111]:443\n"
                b"2001:db8::1\n")          # 文档保留段，is_global=False → 必须丢弃
        self._fetch_and_check(body, ["104.16.0.1", "2606:4700::1111"])

    def test_F_fetch_http_404_keeps_ipfile(self):
        """HTTP 失败 → 契约退出码 1，且 <ipfile> 逐字节保持原样。"""
        d = tempfile.mkdtemp(prefix="cfopt-f404-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ipfile = os.path.join(d, "cfopt-ips.txt")
        before = b"9.9.9.9\n"
        write_bytes(ipfile, before)
        url = self._serve(b"not found\n", status=404)
        rc, log = self.h.run(["fetch", url, ipfile], "yaml")
        self.assertEqual(rc, 1, "F: HTTP 失败时 fetch 必须退出码 1（日志：%s）" % log.strip())
        self.assertEqual(read_bytes(ipfile), before, "F: HTTP 失败时 <ipfile> 必须逐字节保持原样")

    def test_F_measure_success_local(self):
        """measure 成功路径：本地 443 可达 → exit 0、写 ipfile（纯 IP、LF）与 history（LF）。

        `127.0.0.1/32` 的候选集只会是 127.0.0.1（span=0），所以本用例不依赖随机采样。
        """
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 443))
            srv.listen(16)
        except OSError as exc:
            srv.close()
            self.skipTest("本机 443 端口不可用，跳过 measure 成功路径：%s" % exc)
        self.addCleanup(srv.close)
        stop = threading.Event()

        def _accept():
            srv.settimeout(0.3)
            while not stop.is_set():
                try:
                    c, _ = srv.accept()
                except (socket.timeout, OSError):
                    continue
                try:
                    c.close()
                except OSError:
                    pass

        th = threading.Thread(target=_accept, daemon=True)
        th.start()
        self.addCleanup(stop.set)

        d = tempfile.mkdtemp(prefix="cfopt-fm-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ipfile = os.path.join(d, "cfopt-ips.txt")
        history = os.path.join(d, "cfopt-history.txt")
        write_bytes(ipfile, b"9.9.9.9\n")
        rc, log = self.h.run(["measure", "127.0.0.1/32", ipfile, "cloudflare.com",
                              "4", "4", "500", "false", history, "4"], "yaml")
        self.assertEqual(rc, 0, "F: measure 成功路径应退出码 0（日志：%s）" % log.strip())
        raw = read_bytes(ipfile)
        self.assertIsNotNone(raw, "F: measure 成功必须写 <ipfile>")
        lines = norm_bytes(raw).decode("utf-8", "replace").splitlines()
        self.assertTrue(lines, "F: measure 成功的 <ipfile> 不应为空")
        for ln in lines:
            try:
                ipaddress.ip_address(ln.strip())
            except ValueError:
                self.fail("F: measure 的 <ipfile> 出现非 IP 行 %r" % ln)
        self.assertNotIn(b"\r", raw, "F: measure 写 <ipfile> 必须 LF")
        hraw = read_bytes(history)
        self.assertIsNotNone(hraw, "F: measure 成功必须写 <history>")
        self.assertNotIn(b"\r", hraw, "F: measure 写 <history> 必须 LF（append 也要 newline='\\n'）")


# ---------------------------------------------------------------- G 组：CRLF 输入

class GCRLF(Base):
    """G. 额外：CRLF 输入 → LF 输出（universal newlines 契约），以及**写文件一律 LF**。

    lead 2026-10-19：实现 5 处写文件全部显式 ``newline="\\n"``，所以这里由「只告警」
    收紧为**硬断言**：任何产物里出现 ``\\r`` 都算失败。
    """

    def test_G_crlf_input_same_as_lf(self):
        lf = read_bytes(os.path.join(FIXDIR, "f8.yaml"))
        crlf = lf.replace(b"\n", b"\r\n")
        self.assertIn(b"\r\n", crlf, "G: 构造的 CRLF 输入无效")
        a = self.h.inject_case("f8", fixture="f8")
        b = self.h.inject_case("crlf-f8", raw=crlf)
        self.assertEqual(norm_bytes(a["new_bytes"]), norm_bytes(b["new_bytes"]),
                         "G: CRLF 输入与 LF 输入必须产出相同（归一化后）的输出")
        self.assertNotIn(b"\r", b["new_bytes"],
                         "G: CRLF 输入经 universal newlines 读入后，写出必须仍是纯 LF")
        self.assertEqual(b["cfg_bytes"], crlf, "G: 绝不改动 <cfg> 本身（CRLF 输入亦然）")

    def test_G_every_written_file_is_lf(self):
        """所有我们观察得到的产物都必须是 LF：候选、片段、fetch 的 ipfile。"""
        r = self.h.inject_case("f8-lf", fixture="f8", pre_snippet=True, snippet_delta=-3600.0)
        self.assertIsNotNone(r["new_bytes"], "G: 必须有候选产物")
        self.assertNotIn(b"\r", r["new_bytes"], "G: <cfg>.cfopt-new 必须是 LF")
        self.assertIsNotNone(r["snippet_bytes"], "G: 成功注入必须写 <snippet>")
        self.assertNotIn(b"\r", r["snippet_bytes"], "G: <snippet> 必须是 LF")
        # fetch 成功写出的 ipfile（本地 HTTP，不依赖外网）
        d = tempfile.mkdtemp(prefix="cfopt-glf-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ipfile = os.path.join(d, "ips.txt")
        write_bytes(ipfile, b"9.9.9.9\n")
        httpd = HTTPServer(("127.0.0.1", 0), _BlobHandler)
        httpd.RequestHandlerClass.body = b"104.16.0.1\n2606:4700::1111\n"
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        rc, log = self.h.run(["fetch", "http://127.0.0.1:%d/blob" % httpd.server_address[1], ipfile])
        self.assertEqual(rc, 0, "G: fetch 应成功（日志：%s）" % log.strip())
        self.assertNotIn(b"\r", read_bytes(ipfile), "G: fetch 的 <ipfile> 必须是 LF")


# ---------------------------------------------------------------- H. settings.env 读写闭环
# 背景（2026-10-19 真机事故）：cmd/config_callback 为了防 `. settings.env` 命令注入，把值改成
# **单引号包裹**（`MIHOMO_API_SECRET='${secret}'`），但 app/ui_server.py 的 read_secret() 仍按
# `split("=")[1].strip()` 取值 → 把引号一起当成了密钥 → 对 Clash API 的请求全部 401
# （现象：面板/状态页显示「后端不可达」，而内核本身完全正常）。
# 教训：**改写入方必须同时改读方**。这里把两侧的契约钉死，防止再犯。

def _load_unquote_fn():
    """加载 ui_server.py 里的 settings.env **读取链路**，不 import 整个模块。

    整个模块在 Windows 上 import 会因 ``socket.AF_UNIX`` 不存在而失败，所以按 AST
    取相关函数体单独 exec —— 只测纯函数，避免平台耦合。

    返回 ``(read_secret, unquote_env_value)``：必须连 ``read_secret`` 一起取，
    否则只能测到辅助函数、抓不到「read_secret 忘了调用它」这种真实回归
    （本组第一版就犯了这个错：变异掉 read_secret 的调用后测试仍然全绿）。
    """
    import ast
    path = os.path.join(ROOT, "app", "ui_server.py")
    tree = ast.parse(read_bytes(path).decode("utf-8"))
    want = ("unquote_env_value", "read_secret")
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want]
    if len(fns) != len(want):
        return None, None
    ns = {"os": os}
    exec(compile(ast.Module(body=fns, type_ignores=[]), path, "exec"), ns)
    return ns.get("read_secret"), ns.get("unquote_env_value")


class HSettingsEnvRoundTrip(unittest.TestCase):
    """H. settings.env 的写入方与读取方必须对「引号」理解一致。"""

    def setUp(self):
        self.read_secret, self.unquote = _load_unquote_fn()
        if self.unquote is None or self.read_secret is None:
            self.skipTest("ui_server.py 里没有 unquote_env_value / read_secret")

    def _read_secret_from(self, text):
        """把 settings.env 写到临时目录，让**真实的 read_secret()** 去读它。"""
        d = tempfile.mkdtemp(prefix="cfopt-secret-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        write_bytes(os.path.join(d, "settings.env"), text.encode("utf-8"))
        # read_secret 用模块级 DATA_DIR 拼路径，这里把它指到临时目录
        self.read_secret.__globals__["DATA_DIR"] = d
        return self.read_secret()

    def test_H0_read_secret_actually_strips_quotes(self):
        """端到端钉住「真机上出问题的那条路径」：read_secret() 必须剥掉引号。

        这一条是本组的核心防线 —— H2 只测辅助函数，若 read_secret **忘了调用**它
        （真实事故就是这样），只有本用例会红。
        """
        got = self._read_secret_from("MIHOMO_API_SECRET='21253611'\n")
        self.assertEqual(got, "21253611",
                         "H0: read_secret() 必须剥掉写入方的单引号，否则 Clash API 全部 401")
        self.assertNotEqual(got, "'21253611'", "H0: 绝不能把引号当成密钥的一部分")
        # 兼容旧版无引号写法（升级路径：老用户的 settings.env 没有引号）
        self.assertEqual(self._read_secret_from("MIHOMO_API_SECRET=21253611\n"), "21253611",
                         "H0: 升级前遗留的无引号值必须仍能读到")
        # 其它键、缺键、空文件都不能炸
        self.assertEqual(self._read_secret_from("MIHOMO_MIXED_PORT=7890\n"), "",
                         "H0: 没有该键时返回空串")
        self.assertEqual(self._read_secret_from(""), "", "H0: 空文件返回空串")

    def test_H1_writer_quotes_reader_strips(self):
        """写入方确实加了单引号（若这条挂了，说明写入方改了而本测试过期）。"""
        src = read_bytes(os.path.join(ROOT, "cmd", "config_callback")).decode("utf-8")
        self.assertRegex(src, r"(?m)^MIHOMO_API_SECRET='\$\{secret\}'$",
                         "H1: config_callback 必须仍以单引号写 MIHOMO_API_SECRET")
        self.assertRegex(src, r"(?m)^MIHOMO_SUBSCRIPTION_URL='\$\{sub\}'$",
                         "H1: config_callback 必须仍以单引号写 MIHOMO_SUBSCRIPTION_URL")

    def test_H4_domain_add_field_name_matches_wizard(self):
        """向导暴露的字段名必须被 config_callback 认到，否则「追加域名」静默失效。

        2026-11-13 真机：wizard/config 里字段叫 `wizard_cfopt_domain_add`，而
        config_callback 只读 `wizard_cfopt_domains` —— 表单里填的域名永远进不了
        cfopt-domains.txt，注入的规则里自然也没有它（现象是「功能看着开着，域名没生效」）。
        """
        import json                      # 本模块其它用例不依赖 json，就近导入即可

        src = read_bytes(os.path.join(ROOT, "cmd", "config_callback")).decode("utf-8")
        self.assertIn("wizard_cfopt_domain_add", src,
                      "H4: config_callback 必须认向导真实字段 wizard_cfopt_domain_add")
        self.assertIn("wizard_cfopt_domains", src,
                      "H4: 同时保留旧字段 wizard_cfopt_domains 的兼容（手工/老版本）")
        wiz = json.loads(read_bytes(os.path.join(ROOT, "wizard", "config")).decode("utf-8"))
        fields = {it.get("field") for step in wiz for it in step["items"] if it.get("field")}
        self.assertIn("wizard_cfopt_domain_add", fields,
                      "H4: 向导必须继续暴露 wizard_cfopt_domain_add")

    def test_H2_reader_strips_exactly_one_layer(self):
        """读取方按 shell 语义剥**一层**成对引号；未加引号的旧值不受影响。"""
        cases = [
            ("'21253611'", "21253611"),   # 本批改动后的真实写法 = 事故现场
            ("21253611", "21253611"),     # 升级前遗留的无引号值必须仍可用
            ('"a,b"', "a,b"),             # 双引号同样剥一层
            ("''", ""),                   # 空值
            ("  'x'  ", "x"),             # 行尾空白
            ("'unclosed", "'unclosed"),   # 不成对 → 原样返回，不静默吞字符
        ]
        for raw, want in cases:
            self.assertEqual(self.unquote(raw), want,
                             "H2: unquote_env_value(%r) 必须是 %r（与 shell 一致）" % (raw, want))

    def test_H3_quirky_secret_survives_shell_and_reader(self):
        """带 shell 元字符的密钥：`. settings.env` 后必须与读取方**得到同一个值**。

        这是本组最重要的断言：用真实 shell 逐字节对比，而不是我手写期望值。
        """
        if not shutil.which("bash"):
            self.skipTest("没有 bash，无法执行真实 shell 对照")
        d = tempfile.mkdtemp(prefix="cfopt-env-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        env = os.path.join(d, "settings.env")
        # 与 config_callback 同样的写法：单引号包裹。值里含 $ & ; ` 和空格。
        secret = "a$b&c;d`e f"
        write_bytes(env, ("MIHOMO_API_SECRET='%s'\n" % secret).encode("utf-8"))
        # 真实 shell 的行为
        got = subprocess.run(["bash", "-c", '. "$1"; printf "%s" "$MIHOMO_API_SECRET"', "_", env],
                             capture_output=True)
        shell_val = got.stdout.decode("utf-8")
        self.assertEqual(shell_val, secret, "H3: `. settings.env` 应还原出原始密钥")
        # 读取方的行为（读的是同一行）
        line = read_bytes(env).decode("utf-8").splitlines()[0]
        self.assertEqual(self.unquote(line.split("=", 1)[1]), shell_val,
                         "H3: ui_server 读到的密钥必须与 shell 完全一致")
        # 反向自证：若读取方不剥引号（旧实现），两者必然不同 —— 证明本测试真能抓到回归
        naive = line.split("=", 1)[1].strip()
        self.assertNotEqual(naive, shell_val,
                            "H3: 旧实现（不剥引号）本应与 shell 不一致，否则本测试是空跑")


# ---------------------------------------------------------------- I. scan（识别套 CF 的站点）
class IScan(Base):
    """I. ``scan`` 子命令：判定哪些域名落在 CF 官方网段。

    背景：优选注入靠顶层 ``hosts``，而 hosts 只能按字面域名匹配（没有「匹配所有域名」
    的通配符）。所以「凡是套 CF 的站点自动生效」不可实现；能自动化的只有「找出哪些
    域名套了 CF」这一步，由用户挑选后填进域名清单。

    本组**不依赖外网**：只断言参数校验、片段解析、输出格式与退出码；真实 DNS 解析
    另有一条可选用例（无网络时自动跳过）。
    """

    def _mk(self, snippet_text=None):
        d = tempfile.mkdtemp(prefix="cfopt-scan-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        snip = os.path.join(d, "snippet.txt")
        out = os.path.join(d, "out.txt")
        if snippet_text is not None:
            write_bytes(snip, snippet_text.encode("utf-8"))
        return snip, out

    def test_I1_usage_when_no_args(self):
        rc, log = self.h.run(["scan"])
        self.assertEqual(rc, 1, "I1: 缺参数必须退出码 1（日志：%s）" % log.strip())
        self.assertIn("用法", log, "I1: 缺参数应打印用法")

    def test_I2_empty_candidates_writes_header_and_exits_0(self):
        snip, out = self._mk()          # 片段不存在
        rc, log = self.h.run(["scan", snip, out])
        self.assertEqual(rc, 0, "I2: 没有候选域名不算错误（日志：%s）" % log.strip())
        self.assertTrue(os.path.exists(out), "I2: 即使没有候选也要写输出文件")
        self.assertIn("没有可扫描的域名", log, "I2: 应说明为什么没结果")

    def test_I3_write_failure_exits_1(self):
        snip, _out = self._mk()
        bad = os.path.join(tempfile.gettempdir(), "cfopt-no-such-dir-%d" % os.getpid(),
                           "out.txt")
        rc, log = self.h.run(["scan", snip, bad, "example.com"])
        self.assertEqual(rc, 1, "I3: 输出不可写必须退出码 1（日志：%s）" % log.strip())

    def test_I4_snippet_is_parsed_and_deduped(self):
        """片段里的域名要被读出来（大小写归一、去掉前导点），且与额外域名去重。"""
        snip, out = self._mk(
            "# cfopt:hosts\n"
            "162.159.58.17\tWWW.Cloudflare.COM\n"
            "162.159.58.18\t.example.org\n")
        # 额外域名里重复给一个（大小写不同），应只出现一次
        rc, log = self.h.run(["scan", snip, out, "www.cloudflare.com,www.baidu.com"])
        self.assertEqual(rc, 0, "I4: 扫描应成功（日志：%s）" % log.strip())
        body = read_bytes(out).decode("utf-8")
        self.assertIn("www.cloudflare.com", body.lower(), "I4: 片段里的域名必须被扫描")
        self.assertIn("example.org", body, "I4: 前导点应被去掉")
        # 每个域名在输出里只能出现一行（去重）
        names = [ln.split("\t")[0] for ln in body.splitlines()
                 if ln and not ln.startswith("#")]
        self.assertEqual(len(names), len(set(names)), "I4: 输出不得有重复域名: %s" % names)
        self.assertEqual(len(names), 3, "I4: 应有 3 个不同域名，实际 %s" % names)

    def test_I5_output_columns_and_lf(self):
        snip, out = self._mk()
        rc, _log = self.h.run(["scan", snip, out, "www.baidu.com"])
        self.assertEqual(rc, 0)
        raw = read_bytes(out)
        self.assertNotIn(b"\r", raw, "I5: 输出必须是 LF")
        self.assertTrue(raw.endswith(b"\n"), "I5: 输出应有尾换行")
        data = [ln for ln in raw.decode("utf-8").splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(len(data), 1)
        cols = data[0].split("\t")
        self.assertEqual(len(cols), 4, "I5: 每行必须是 4 列（域名/ v4数 / v6数 / 结论）: %r"
                         % data[0])
        self.assertEqual(cols[0], "www.baidu.com")
        self.assertIn(cols[3], ("套 CF", "非 CF", "解析失败"),
                      "I5: 结论列取值必须受控: %r" % cols[3])

    def test_I6_cf_verdict_matches_official_ranges(self):
        """真实解析：cloudflare.com 必须判为套 CF，baidu.com 必须判为非 CF。

        无网络/解析失败时跳过，避免 CI 抖动。"""
        snip, out = self._mk()
        rc, log = self.h.run(["scan", snip, out, "www.cloudflare.com,www.baidu.com"])
        self.assertEqual(rc, 0, "I6: 扫描应成功（日志：%s）" % log.strip())
        verdicts = {}
        for ln in read_bytes(out).decode("utf-8").splitlines():
            if ln and not ln.startswith("#"):
                cols = ln.split("\t")
                verdicts[cols[0]] = cols[3]
        if verdicts.get("www.cloudflare.com") == "解析失败":
            self.skipTest("DNS 不可用，跳过真实解析断言")
        self.assertEqual(verdicts.get("www.cloudflare.com"), "套 CF",
                         "I6: www.cloudflare.com 必须判为套 CF")
        self.assertEqual(verdicts.get("www.baidu.com"), "非 CF",
                         "I6: www.baidu.com 必须判为非 CF")


# ---------------------------------------------------------------- J. 按域名选 IP
# 背景（2026-10-19 真机实测）：Cloudflare 限速按「IP + 站点」计，同一 IP 对不同域名
# 可差一个数量级（104.20.17.84 对 hdtime.org 的 TLS 0.28s，对 ourbits.club 3.95s）。
# 一份全局列表必然让部分站点吃亏，故新增 map 子命令按域名各选各的 IP。

class JDomainMap(Base):
    """J. ``map`` 产出映射 + ``inject`` 按映射取 IP；无映射时行为完全不变。"""

    def _inject_with_map(self, domains, ipfile_text, map_text=None, top="4"):
        d = tempfile.mkdtemp(prefix="cfopt-j-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        cand = os.path.join(d, "cfg.yaml")
        write_bytes(cand, b"hosts:\nproxies: []\nrules:\n  - 'MATCH,DIRECT'\n")
        ipfile = os.path.join(d, "ips.txt")
        write_bytes(ipfile, ipfile_text.encode("utf-8"))
        if map_text is not None:
            write_bytes(ipfile + ".map", map_text.encode("utf-8"))
        snip = os.path.join(d, "snip.txt")
        rc, log = self.h.run(["inject", cand, ipfile, domains, top, snip])
        return rc, log, read_bytes(cand).decode("utf-8")

    def _hosts_ips(self, text):
        """从注入结果里取出 {域名: [ip...]}。"""
        out, cur = {}, None
        for ln in text.splitlines():
            m = re.match(r"\s*'\+\.([^']+)':.*cfopt:hosts", ln)
            if m:
                cur = m.group(1)
                out[cur] = []
                continue
            m = re.match(r"\s*-\s*([0-9a-fA-F:.]+)\s.*cfopt:hosts", ln)
            if m and cur:
                out[cur].append(m.group(1))
        return out

    def test_J1_map_is_authoritative_unmapped_is_skipped(self):
        """有映射文件时，映射就是权威：没映射的域名**跳过注入**（不回退全局）。

        2026-11-13 真机事故：272 域名时 map 选不出 IP 的域名被硬塞全局 IP，hdwing /
        ourbits / 1ptba 等站点 TLS 直接失败。改成「宁可不注入，也不塞坏 IP」。
        """
        rc, log, text = self._inject_with_map(
            "a.com,b.com", "1.1.1.1\n2.2.2.2\n", "a.com\t9.9.9.9 8.8.8.8\n")
        self.assertEqual(rc, 0, "J1: 注入应成功（日志：%s）" % log.strip())
        got = self._hosts_ips(text)
        self.assertEqual(got.get("a.com"), ["9.9.9.9", "8.8.8.8"],
                         "J1: 有映射的域名必须用自己的 IP，实际 %r" % got.get("a.com"))
        self.assertNotIn("b.com", got,
                         "J1: 有映射文件时，没映射的域名必须跳过（不回退全局），实际 %r" % got)
        self.assertIn("严格模式", log, "J1: 日志要说明这是严格模式跳过（日志：%s）" % log.strip())
        rules = [m.group(1) for m in re.finditer(r"DOMAIN-SUFFIX,([^,]+),DIRECT", text)]
        self.assertIn("a.com", rules, "J1: 注入的域名必须有置顶 DIRECT 规则")
        self.assertNotIn("b.com", rules, "J1: 被跳过的域名不能留下 DIRECT 规则")

    def test_J2_without_map_behaves_as_before(self):
        rc, log, text = self._inject_with_map("a.com,b.com", "1.1.1.1\n2.2.2.2\n")
        self.assertEqual(rc, 0, "J2: 注入应成功（日志：%s）" % log.strip())
        got = self._hosts_ips(text)
        for d in ("a.com", "b.com"):
            self.assertEqual(got.get(d), ["1.1.1.1", "2.2.2.2"],
                             "J2: 无映射时 %s 应用全局列表，实际 %r" % (d, got.get(d)))

    def test_J3_map_rejects_non_global_addresses(self):
        """映射里的内网/回环地址必须丢弃；丢弃后该域名等同「无映射」→ 整次不注入。"""
        rc, log, text = self._inject_with_map(
            "a.com,b.com", "1.1.1.1\n", "a.com\t127.0.0.1 10.0.0.1 192.168.1.1\n")
        self.assertEqual(rc, 0, "J3: 应正常退出（日志：%s）" % log.strip())
        got = self._hosts_ips(text)
        self.assertNotIn("a.com", got, "J3: 非公网地址不能被当成映射写进 hosts，实际 %r" % got)
        self.assertNotIn("cfopt:", text,
                         "J3: 一个可用映射都没有时不该留下任何注入痕迹")
        self.assertIn("没有任何域名拿到可用映射", log,
                      "J3: 日志要说明保持原配置（日志：%s）" % log.strip())

    def _map_run(self, domains_arg, ips_text, pre_map=None, age_sec=0):
        d = tempfile.mkdtemp(prefix="cfopt-j6-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ipfile = os.path.join(d, "ips.txt")
        write_bytes(ipfile, ips_text.encode("utf-8"))
        out = os.path.join(d, "m.txt")
        if pre_map is not None:
            write_bytes(out, pre_map.encode("utf-8"))
            if age_sec:
                old = time.time() - age_sec
                os.utime(out, (old, old))
        rc, log = self.h.run(["map", ipfile, domains_arg, out, "2", "8"])
        return rc, log, read_bytes(out).decode("utf-8")

    @staticmethod
    def _sig(domains_arg):
        import hashlib
        doms = [x for x in domains_arg.split(",") if x]
        return hashlib.sha1(",".join(doms).encode("utf-8")).hexdigest()[:16]

    def test_J6_fresh_map_is_reused(self):
        """域名指纹一致且未过期 → 复用，不重新实测（这条是「启动别卡几分钟」的保障）。"""
        sig = self._sig("a.com,b.com")
        pre = "# map-sig: %s\na.com\t9.9.9.9 8.8.8.8\n" % sig
        # 候选用文档网段（不可达）：若没有复用就会去实测，结果会丢掉 a.com 这一行
        rc, log, text = self._map_run("a.com,b.com", "203.0.113.9\n", pre_map=pre, age_sec=60)
        self.assertEqual(rc, 0, "J6: 应正常退出（日志：%s）" % log.strip())
        self.assertIn("复用按域名映射", log, "J6: 应当复用（日志：%s）" % log.strip())
        self.assertIn("a.com\t9.9.9.9", text, "J6: 复用时映射内容不能被改写")

    def test_J7_expired_or_changed_domains_remeasured(self):
        """过期或域名集合变化 → 必须重测（不能拿旧映射糊弄）。"""
        sig = self._sig("a.com,b.com")
        pre = "# map-sig: %s\na.com\t9.9.9.9 8.8.8.8\n" % sig
        # 1) 过期（13 小时 > 默认 12h）
        rc, log, text = self._map_run("a.com,b.com", "203.0.113.9\n", pre_map=pre, age_sec=13 * 3600)
        self.assertEqual(rc, 0)
        self.assertNotIn("复用按域名映射", log, "J7: 过期必须重测（日志：%s）" % log.strip())
        self.assertNotIn("9.9.9.9", text, "J7: 重测后不能残留旧映射")
        self.assertIn("# map-sig: %s" % sig, text, "J7: 重测后要写回新的指纹")
        # 2) 未过期但域名集合变了
        rc, log, text = self._map_run("a.com,c.com", "203.0.113.9\n", pre_map=pre, age_sec=60)
        self.assertEqual(rc, 0)
        self.assertNotIn("复用按域名映射", log, "J7: 域名变了必须重测（日志：%s）" % log.strip())

    def test_J4_map_usage_and_exit_codes(self):
        rc, log = self.h.run(["map"])
        self.assertEqual(rc, 1, "J4: 缺参数应退出码 1（日志：%s）" % log.strip())
        self.assertIn("用法", log, "J4: 缺参数应打印用法")
        d = tempfile.mkdtemp(prefix="cfopt-j4-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        out = os.path.join(d, "m.txt")
        rc, log = self.h.run(["map", os.path.join(d, "none.txt"), "a.com", out, "2", "4"])
        self.assertEqual(rc, 0, "J4: 无候选 IP 应退出码 0（日志：%s）" % log.strip())
        self.assertTrue(os.path.exists(out), "J4: 即使没有候选也要写映射文件")
        self.assertEqual(read_bytes(out), b"", "J4: 无候选时应产出空映射")

    def test_J5_skips_when_no_usable_candidate(self):
        """候选全是非公网地址时，不应产出任何映射（防止把回环地址写进 hosts）。"""
        d = tempfile.mkdtemp(prefix="cfopt-j5-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ipfile = os.path.join(d, "ips.txt")
        write_bytes(ipfile, b"127.0.0.1\n10.0.0.1\n")
        out = os.path.join(d, "m.txt")
        rc, log = self.h.run(["map", ipfile, "a.com", out, "2", "4"])
        self.assertEqual(rc, 0, "J5: 应正常完成（日志：%s）" % log.strip())
        # 候选非公网时会被当成「没有候选」，产出空映射。
        # 注意：空映射文件里允许只有 `# map-sig:` 指纹注释行（供 TTL 复用判断），
        # 断言只看**非注释行**——那才是真正会被 inject 读成映射的内容。
        body = [ln for ln in read_bytes(out).decode("utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
        self.assertEqual(body, [], "J5: 不该为不可用候选产出任何映射条目，实际 %r" % body)


# ---------------------------------------------------------------- K. 域名清单文件
# 背景：域名清单从「应用设置」搬到独立文件。向导 text 字段上限 1023 字节（79 个域名
# 已 929 字节），且表单提交是整份替换 —— 浏览器自动填充的旧值会把清单冲掉（真机踩过）。
# 改成文件后无长度上限，且表单只做「追加」，最坏是重复添加而不是清空。

class KDomainList(Base):
    """K. ``domains`` 子命令：list / add / rm / set 与校验。"""

    def _f(self):
        d = tempfile.mkdtemp(prefix="cfopt-k-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return os.path.join(d, "cfopt-domains.txt")

    def _lines(self, p):
        return [l for l in read_bytes(p).decode("utf-8").splitlines()
                if l.strip() and not l.startswith("#")]

    def test_K1_add_appends_and_dedupes(self):
        p = self._f()
        rc, log = self.h.run(["domains", p, "add", "b.com,a.com"])
        self.assertEqual(rc, 0, "K1: add 应成功（日志：%s）" % log.strip())
        rc, _ = self.h.run(["domains", p, "add", "a.com,c.com"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._lines(p), ["a.com", "b.com", "c.com"],
                         "K1: 追加应去重并排序，实际 %r" % self._lines(p))

    def test_K2_add_never_removes_existing(self):
        """这是本次改造的核心：表单提交只能是「追加」，绝不能冲掉已有清单。"""
        p = self._f()
        self.h.run(["domains", p, "set", "a.com,b.com,c.com"])
        rc, _ = self.h.run(["domains", p, "add", "only-one.com"])
        self.assertEqual(rc, 0)
        got = self._lines(p)
        for d in ("a.com", "b.com", "c.com", "only-one.com"):
            self.assertIn(d, got, "K2: add 之后 %s 必须还在，实际 %r" % (d, got))

    def test_K3_rm_and_set(self):
        p = self._f()
        self.h.run(["domains", p, "set", "a.com,b.com,c.com"])
        rc, log = self.h.run(["domains", p, "rm", "b.com"])
        self.assertEqual(rc, 0, "K3: rm 应成功（日志：%s）" % log.strip())
        self.assertEqual(self._lines(p), ["a.com", "c.com"], "K3: rm 只该删指定域名")
        rc, _ = self.h.run(["domains", p, "set", "x.com"])
        self.assertEqual(self._lines(p), ["x.com"], "K3: set 应整份替换")

    def test_K4_invalid_domains_rejected(self):
        p = self._f()
        # 逗号/空格/分号都算分隔符（用户习惯不同），所以 `a b.com` 会拆成 a(非法) + b.com(合法)
        rc, log = self.h.run(["domains", p, "add", "ok.com,非法域名!,a b.com"])
        self.assertEqual(rc, 0, "K4: 有合法项时应成功（日志：%s）" % log.strip())
        self.assertEqual(self._lines(p), ["b.com", "ok.com"],
                         "K4: 只该保留合法域名，实际 %r" % self._lines(p))
        # 全是非法项 → 失败
        rc, _ = self.h.run(["domains", p, "add", "非法!,x y"])
        self.assertEqual(rc, 1, "K4: 全非法时应返回 1")

    def test_K5_list_and_usage(self):
        p = self._f()
        self.h.run(["domains", p, "set", "b.com,a.com"])
        out = subprocess.run([sys.executable, BIN, "domains", p, "list"],
                             cwd=ROOT, capture_output=True)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.decode("utf-8").split(), ["a.com", "b.com"],
                         "K5: list 应按顺序输出全部域名")
        rc, log = self.h.run(["domains"])
        self.assertEqual(rc, 1, "K5: 缺参数应退出码 1（日志：%s）" % log.strip())
        self.assertIn("用法", log)
        rc, _ = self.h.run(["domains", p, "bogus"])
        self.assertEqual(rc, 1, "K5: 未知操作应退出码 1")


# ---------------------------------------------------------------- 自检（默认跳过）

class SelfCheck(Base):
    """默认跳过的自检：故意断言一个错的键名，用来证明本套件真的会红。"""

    def setUp(self):
        if not SELFCHECK:
            self.skipTest("自检默认关闭：用 --self-check 或 CFOPT_TEST_SELFCHECK=1 打开")
        self.h = HARNESS

    def test_SELFCHECK_f4_wrong_key_must_fail(self):
        """自检：对 f4 断言一个不存在的键 —— 这条断言必须失败。"""
        keys = [KEY]
        try:
            r = self.h.inject_case("f4", fixture="f4")
            keys = list(hosts_of(yload(norm_text(r["new_bytes"].decode("utf-8", "replace")))).keys())
        except Exception:
            pass
        self.assertIn("this-key-must-never-appear.example.com", keys,
                      "自检：这条断言被故意写错；若它通过了，说明套件在空跑（keys=%r）" % keys)


# ---------------------------------------------------------------- 汇总运行器

class RecordingResult(unittest.TextTestResult):
    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.outcomes = {}

    def startTest(self, test):
        super().startTest(test)
        self.outcomes[test.id()] = "?"

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes[test.id()] = "ok"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcomes[test.id()] = "FAIL"

    def addError(self, test, err):
        super().addError(test, err)
        self.outcomes[test.id()] = "ERROR"

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.outcomes[test.id()] = "skip"

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.outcomes[test.id()] = "xfail"

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.outcomes[test.id()] = "xpass"


def group_of(tid):
    name = tid.rsplit(".", 1)[-1]
    m = re.match(r"test_([A-Za-z]+)[0-9]*_", name)
    return m.group(1) if m else "?"


def main(argv):
    global SELFCHECK, STRICT_CONTRACT
    if "--self-check" in argv:
        SELFCHECK = True
    if "--strict-contract" in argv:
        STRICT_CONTRACT = True
    unknown = [a for a in argv if a not in ("--self-check", "--strict-contract")]
    if unknown:
        print("未知参数：%r（可用：--self-check / --strict-contract）" % unknown)
        return 2

    print("=" * 72)
    print("cfopt 黑盒回归测试套件（task-1）")
    print("  被测实现 : %s%s" % (BIN, "" if os.path.exists(BIN) else "   [不存在]"))
    print("  仓库根   : %s" % ROOT)
    print("  夹具目录 : %s (%d 个)" % (FIXDIR, len([f for f in os.listdir(FIXDIR) if f.endswith('.yaml')])))
    print("  PyYAML   : %s" % (("可用 %s" % getattr(yaml, "__version__", "?")) if HAVE_YAML else ("缺失：%s" % YAML_ERR)))
    print("  self-check=%s  strict-contract=%s  PYTHONPATH 屏蔽目录=%s"
          % (SELFCHECK, STRICT_CONTRACT, NOYAML))
    print("  inject argv 语义 : 期望=%s（lead 裁决：argv[1]=<cfg>.cfopt-new 候选）" % ARGV_MODE)
    try:
        m = HARNESS.mode("yaml")
    except Exception as exc:
        m = "probe-error: %s" % exc
    print("  实际生效        : %s" % m)
    if not os.path.exists(BIN):
        print("  [PRECHECK] 待测实现不存在 —— 全部端到端用例都会红（契约驱动，待 lead 冻结后联调）")
    elif m not in ("candidate", "literal"):
        print("  [PRECHECK] inject 的 argv[1] 语义不符契约口径 —— 契约歧义，需 lead 复核")
    elif m == "literal":
        print("  [PRECHECK] 注意：实现按 literal 口径（argv[1]=原配置），与 lead 裁决的 candidate 口径不同")
    print("=" * 72)

    if not HAVE_YAML:
        print("[FATAL] 测试自身需要 PyYAML 校验产物；当前环境缺失，无法执行契约断言。")
        return 3

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult, stream=sys.stdout)
    result = runner.run(suite)

    groups = {}
    for tid, status in result.outcomes.items():
        g = group_of(tid)
        groups.setdefault(g, Counter())[status] += 1

    print("")
    print("=" * 72)
    print("cfopt_test 汇总")
    print("-" * 72)
    order = ["A", "B", "C", "D", "E", "F", "G", "SELFCHECK"]
    keys = order + [g for g in sorted(groups) if g not in order]
    total_ok = total_bad = total_skip = 0
    for g in keys:
        if g not in groups:
            continue
        c = groups[g]
        ok = c.get("ok", 0) + c.get("xfail", 0)
        bad = c.get("FAIL", 0) + c.get("ERROR", 0) + c.get("xpass", 0)
        skip = c.get("skip", 0)
        total_ok += ok
        total_bad += bad
        total_skip += skip
        label = {"A": "夹具契约", "B": "行为不变量", "C": "流式对拍",
                 "D": "双路径一致", "E": "失败回退", "F": "fetch 成功(本地)",
                 "G": "CRLF 输入", "SELFCHECK": "自检"}.get(g, g)
        print("  %-9s %-14s 通过 %2d  失败 %2d  跳过 %2d" % (g, label, ok, bad, skip))
    print("-" * 72)
    print("  总计 %d 例：通过 %d，失败 %d，跳过 %d" % (result.testsRun, total_ok, total_bad, total_skip))

    if NOTES:
        print("-" * 72)
        print("  告警 / 契约待裁决：")
        for msg in NOTES:
            print("    - %s" % msg)

    if SELFCHECK:
        selfcheck_failed = any("SELFCHECK" in t for t, s in result.outcomes.items()
                               if s in ("FAIL", "ERROR"))
        if selfcheck_failed:
            print("-" * 72)
            print("  自检通过：故意写错的断言确实红了 —— 本套件不是空跑。")
    print("=" * 72)

    if SELFCHECK:
        return 0 if any("SELFCHECK" in t for t, s in result.outcomes.items()
                        if s in ("FAIL", "ERROR")) else 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        HARNESS.close()
else:  # 被 ``unittest discover`` 以模块方式导入时，也要回收临时目录
    import atexit
    atexit.register(HARNESS.close)