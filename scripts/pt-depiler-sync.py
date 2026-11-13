#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pt-depiler-sync —— 从 PT-depiler 站点定义里同步「套 Cloudflare」的 PT 站点域名。

用途
----
cfopt（Cloudflare 优选 IP）只对**套 CF** 的域名有意义：注入会给域名写 hosts 映射
（优选 IP）并置顶 DOMAIN-SUFFIX,...,DIRECT。若把**非 CF** 站点写进清单，hosts 会把
它解析到 Cloudflare 的 anycast IP —— 站点直接打不开。所以本脚本的核心职责是
「只把真正套 CF 的域名交出去」，而不是无脑全量导入。

数据源
------
https://github.com/pt-plugins/PT-depiler 的 `src/packages/site/definitions/*.ts`，
每个文件一个站点，站点元数据里有 `urls: ["https://example.org/", ...]`。
上游持续增删站点，所以需要定期重跑（见 --report-only 与定时任务）。

为什么走 DoH + 代理
------------------
国内直连解析几百个 PT 域名又慢又容易被污染；统一用 DoH（JSON）经代理查询，
既快又干净。代理留空则直连。

用法
----
    python scripts/pt-depiler-sync.py --out cfopt-domains.new.txt
    python scripts/pt-depiler-sync.py --report-only          # 只出报告，不写文件
    python scripts/pt-depiler-sync.py --proxy http://192.168.0.2:7890

退出码：0=成功 / 1=参数或网络错误。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = "pt-plugins/PT-depiler"
BRANCH = "master"
TARBALL = "https://codeload.github.com/%s/tar.gz/refs/heads/%s" % (REPO, BRANCH)
CF_IPS_API = "https://api.cloudflare.com/client/v4/ips"
# 多个 DoH 端点轮换：单一端点并发高了会被限流（实测 cloudflare-dns 大量 429）。
DOH_ENDPOINTS = [
    "https://cloudflare-dns.com/dns-query",
    "https://dns.alidns.com/resolve",
    "https://doh.pub/dns-query",
]
DOH_RR = {"n": 0}

# Cloudflare 官方网段的兜底副本（2026-10 取自 api.cloudflare.com/client/v4/ips）。
# 正常情况下会联网刷新；接口不可用时才用这份，避免整次同步失败。
CF_V4_FALLBACK = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
]
CF_V6_FALLBACK = [
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
]

URL_RE = re.compile(r"""urls\s*:\s*\[(.*?)\]""", re.S)
QUOTED_RE = re.compile(r"""["']([^"']+)["']""")
# 上游把部分站点域名做了 ROT13（形如 "uggcf://1337k.gb/"），避免仓库里出现明文域名
# （主要是成人站与版权敏感站）。ROT13 是自逆的，所以只在**确认是混淆形态**时解码，
# 否则会把明文域名（例如 "https://13city.org/"）也搅乱。
ROT13_PREFIXES = ("uggcf://", "uggc://")


def rot13(text: str) -> str:
    import codecs
    return codecs.decode(text, "rot13")


def deobfuscate(url: str) -> str:
    low = url.lower()
    if low.startswith(ROT13_PREFIXES):
        return rot13(url)
    return url


def opener(proxy: str | None) -> urllib.request.OpenerDirector:
    handlers: list = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def http_get(op: urllib.request.OpenerDirector, url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with op.open(req, timeout=timeout) as resp:
        return resp.read()


def fetch_definitions(op, workdir: str) -> dict[str, dict]:
    """下载并解包 PT-depiler，返回 {站点 id: {"hosts": [...], "tz": "+0800"}}。"""
    tgz = os.path.join(workdir, "ptdepiler.tar.gz")
    with open(tgz, "wb") as fh:
        fh.write(http_get(op, TARBALL, timeout=300))

    sites: dict[str, dict] = {}
    with tarfile.open(tgz, "r:gz") as tar:
        for member in tar.getmembers():
            path = member.name
            if "/src/packages/site/definitions/" not in path:
                continue
            if not path.endswith(".ts"):
                continue
            try:
                text = tar.extractfile(member).read().decode("utf-8", "replace")
            except Exception:
                continue
            m = URL_RE.search(text)
            if not m:
                continue
            idm = re.search(r"""\bid\s*:\s*["']([^"']+)["']""", text)
            site_id = idm.group(1) if idm else os.path.basename(path)[:-3]
            tzm = re.search(r"""timezoneOffset\s*:\s*["']([+-]\d{4})["']""", text)
            # 419 个站点里只有 224 个声明了 timezoneOffset（春天/SpringSunday 就没有），
            # 只认 tz 会把国内站漏掉。兜底判据：`name` / `aka` / `description` 里出现中文。
            # ⚠️ 不能用「整个文件头含中文」：category 块里全是「电影/Movies」这类中文标签，
            #    几乎每个海外站都会命中（实测会误收 198 个站点）。
            ident_fields = " ".join(filter(None, [
                (re.search(r"""\bname\s*:\s*["']([^"']*)["']""", text) or [None, ""])[1],
                (re.search(r"""aka\s*:\s*\[(.*?)\]""", text, re.S) or [None, ""])[1],
                (re.search(r"""description\s*:\s*["']([^"']*)["']""", text) or [None, ""])[1],
            ]))
            has_cjk = re.search(r"[\u4e00-\u9fff]", ident_fields) is not None
            hosts: list[str] = []
            for raw in QUOTED_RE.findall(m.group(1)):
                host = urllib.parse.urlsplit(deobfuscate(raw)).hostname or ""
                host = host.strip().lower()
                if not host:
                    continue
                if host.startswith("www."):
                    host = host[4:]
                if host not in hosts:
                    hosts.append(host)
            if hosts:
                sites[site_id] = {
                    "hosts": hosts,
                    "tz": tzm.group(1) if tzm else "",
                    "cn": (tzm.group(1) == "+0800") if tzm else has_cjk,
                    "_cjk": has_cjk,
                }
    return sites


def cf_ranges(op) -> tuple[list, list]:
    try:
        payload = json.loads(http_get(op, CF_IPS_API, timeout=30).decode("utf-8"))
        v4 = [ipaddress.ip_network(x) for x in payload["result"]["ipv4_cidrs"]]
        v6 = [ipaddress.ip_network(x) for x in payload["result"]["ipv6_cidrs"]]
        print("CF 网段：联网取到 v4 %d / v6 %d 条" % (len(v4), len(v6)))
        return v4, v6
    except Exception as exc:
        print("CF 网段：联网获取失败（%s），改用内置副本" % exc)
        return (
            [ipaddress.ip_network(x) for x in CF_V4_FALLBACK],
            [ipaddress.ip_network(x) for x in CF_V6_FALLBACK],
        )


def resolve(op, name: str, retries: int = 4) -> list[str]:
    """DoH 解析 A / AAAA。端点轮换 + 退避重试，避免单点限流把整批判成「解析失败」。"""
    import time

    out: list[str] = []
    for qtype in (1, 28):                      # 1=A, 28=AAAA（三家 DoH 都认数字）
        for attempt in range(retries):
            DOH_RR["n"] += 1
            base = DOH_ENDPOINTS[DOH_RR["n"] % len(DOH_ENDPOINTS)]
            url = "%s?name=%s&type=%d" % (base, urllib.parse.quote(name), qtype)
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "curl/8",
                    "Accept": "application/dns-json",
                })
                with op.open(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                for ans in data.get("Answer") or []:
                    if ans.get("type") in (1, 28):
                        out.append(ans.get("data", ""))
                break
            except Exception:
                time.sleep(0.2 * (attempt + 1))
    return out


def in_cf(ips: list[str], v4, v6) -> bool:
    for raw in ips:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        for net in (v4 if ip.version == 4 else v6):
            if ip in net:
                return True
    return False


def read_existing(path: str) -> list[str]:
    items: list[str] = []
    if not os.path.exists(path):
        return items
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line not in items:
                items.append(line)
    return items


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="从 PT-depiler 同步套 CF 的 PT 域名清单")
    ap.add_argument("--proxy", default=os.environ.get("PT_SYNC_PROXY", ""),
                    help="HTTP 代理（如 http://192.168.0.2:7890）；留空直连")
    ap.add_argument("--out", default="", help="输出的域名清单路径")
    ap.add_argument("--merge", default="", help="与此清单合并（通常是 NAS 上现有清单的本地副本）")
    ap.add_argument("--report-only", action="store_true", help="只打印报告，不写文件")
    ap.add_argument("--workers", type=int, default=8, help="DoH 并发数")
    ap.add_argument("--include-non-cf", action="store_true",
                    help="连非 CF 站点也输出（危险：会让这些站点被错误解析到 CF，默认关闭）")
    ap.add_argument("--all-cf", action="store_true",
                    help="不按地区筛选，输出全部套 CF 的站点（清单会很长）")
    ap.add_argument("--tz", default="+0800",
                    help="默认只收录该时区的站点域名（默认 +0800，即国内站）；配合 --all-cf 关闭筛选")
    args = ap.parse_args(argv)

    op = opener(args.proxy or None)
    workdir = tempfile.mkdtemp(prefix="ptdepiler-")
    print("工作目录：%s" % workdir)

    sites = fetch_definitions(op, workdir)
    print("PT-depiler 站点定义：%d 个站点" % len(sites))

    # 默认只收国内站（timezoneOffset=+0800）：本应用每个域名都要做一次 TLS 实测选 IP，
    # 域名越多启动越慢（实测 81 个域名 ≈68s/轮，且启动会跑两轮）。全量 385 个域名会把
    # 启动拖到十分钟级，所以「常见 PT」按国内站取舍；要全量用 --all-cf。
    picked_sites = {
        sid: meta for sid, meta in sites.items()
        if args.all_cf or meta.get("cn") or not args.tz
    }
    print("按地区筛选（国内站：tz=+0800 或元数据含中文）后站点：%d 个%s"
          % (len(picked_sites), "（--all-cf 全量）" if args.all_cf else ""))

    domains: list[str] = []
    for meta in picked_sites.values():
        for h in meta["hosts"]:
            if h not in domains:
                domains.append(h)
    print("去重后待判定域名：%d 个" % len(domains))

    v4, v6 = cf_ranges(op)
    verdict: dict[str, tuple[bool, list[str]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(resolve, op, d): d for d in domains}
        done = 0
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                ips = fut.result()
            except Exception:
                ips = []
            verdict[d] = (in_cf(ips, v4, v6), ips)
            done += 1
            if done % 50 == 0:
                print("  已判定 %d/%d" % (done, len(domains)))

    cf_domains = sorted(d for d, (is_cf, ips) in verdict.items() if is_cf)
    non_cf = sorted(d for d, (is_cf, _) in verdict.items() if not is_cf)
    unresolved = sorted(d for d, (_, ips) in verdict.items() if not ips)

    print("\n=== 报告 ===")
    print("套 CF（可进优选清单）：%d" % len(cf_domains))
    print("非 CF（绝不能进清单）：%d" % len(non_cf))
    print("解析失败（本次跳过）：%d" % len(unresolved))
    if unresolved:
        print("  解析失败样例：%s" % ", ".join(unresolved[:10]))

    existing = read_existing(args.merge) if args.merge else []
    merged = list(existing)
    added = [d for d in cf_domains if d not in merged]
    for d in added:
        merged.append(d)
    if args.include_non_cf:
        for d in non_cf:
            if d not in merged:
                merged.append(d)

    print("现有清单 %d 条，本次新增 %d 条，合并后 %d 条" % (len(existing), len(added), len(merged)))
    if added:
        print("新增样例：%s" % ", ".join(sorted(added)[:15]))

    if args.report_only:
        return 0
    if not args.out:
        print("未指定 --out，按 --report-only 处理（未写文件）")
        return 0

    payload = ["# 走 Cloudflare 优选 IP 的域名，每行一个（# 开头为注释）"]
    payload += sorted(merged)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(payload) + "\n")
    print("已写出：%s（%d 条）" % (args.out, len(merged)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))