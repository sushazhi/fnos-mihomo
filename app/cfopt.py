#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cfopt —— Cloudflare 优选 IP 的取数与注入（fnOS mihomo 应用内部工具）。

用法（参数契约由 cmd/main 与 tests/cfopt_test.py 共同冻结）：

    cfopt.py fetch   <url> <ipfile>
    cfopt.py measure <cidrs> <ipfile> <sni> <candidates> <conc> <timeout_ms> <speedtest> <history> <top>
    cfopt.py inject  <cfg> <ipfile> <domains> <top> <snippet>

环境变量：

    CFOPT_MARK   注入行的自识别标记。**真源在 cmd/main 的 export**，这里只保留同值兜底
                 以便单独跑这一段；缺省 "# cfopt:"。

退出码：

    fetch    0=成功写盘 / 1=HTTP 或网络失败 / 2=响应里没有可用的公网 IP
    measure  0=成功写盘 / 2=没有可用网段，或全部候选不可达
    inject   0=正常完成（含「功能未启用，已清除」与「整次放弃」两种情况）/ 1=参数或 IO 错误

为什么是独立文件而不是内嵌 heredoc：

    这三段原先以 heredoc 埋在 cmd/main 里，行结构（缩进、elif 归属）不可见、静态检查
    抓不到、也没有单元测试；本文件把「行为」原样搬出来，回归测试见 tests/cfopt_test.py。

    ⚠️ 各段的实现刻意保持与抽取前逐行一致：参数仍按原来的 sys.argv[N] 取（main() 会把
       sys.argv 重写成等价形态），错误路径仍用 sys.exit(N) 直接退出。

可选的 PyYAML 门禁：

    判断 `hosts:` / `rules:` 的**单行流式**写法时，额外用 PyYAML 校验"库的判读"与
    "手写切分"是否一致（条目数 + 映射的键序），不一致就整次放弃。库只影响"是否放弃"、
    不影响"写出什么"，所以**有库 / 无库两条路径的产物在既有语料上逐字节一致**
    （守这条不变量的是 tests/cfopt_test.py 的 D 组用例）。

    fnOS 实测（2026-10-18，Debian 12.12 / Python 3.11.2 / aarch64）：`python3-yaml
    6.0-3+b2` 确实在底座里，且是 `nfs-kernel-server` 的依赖、与它属于同一个 dpkg
    事务，所以正常 fnOS 上这条门禁是启用的。手写回退仍然保留（防将来精简底座），
    而 `mihomo -t` 始终是最终裁决者。
"""

import os
import sys

try:                      # PyYAML 是可选的「库级门禁」：有它多一层校验，没它退回手写路径
    import yaml as _yaml
except Exception:         # 不存在、编译失败、导入报错 —— 一律视为不可用，绝不因此失败
    _yaml = None


# ══ fetch：从 URL 拉取优选列表，解析成功才覆盖 ipfile ══════════════
def _fetch():
    import ipaddress, os, sys, urllib.request

    url, dst = sys.argv[1], sys.argv[2]
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "clash.meta",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print("cfopt fetch failed:", exc)
        sys.exit(1)


    def parse_cell(cell):
        """从单元格里取出第一个公网 IP（IPv4 / IPv6 都收）；兼容 ip 与 ip:port。

        IPv6 是否生效取决于内核开关。本应用生成的配置不写 ipv6，而内核默认
        顶层 ipv6: true ⇒ executor.updateGeneral 里 resolver.DisableIPv6 = !general.IPv6
        得到 false，即 IPv6 解析是开着的。DIRECT 默认（DualStack）走
        dialer.DialContext(network="tcp") → resolver.LookupIPWithResolver —— 该函数
        命中 hosts 时【直接返回全部 IP（含 IPv6），完全不经过 DisableIPv6】，
        随后 dualStackDialContext 把 v4/v6 各成一组并发竞速，先连上者胜。
        所以 IPv4/IPv6 优选 IP 在默认配置下都会参与；仅当订阅把顶层 ipv6 设为 false、
        且用 ip-version: ipv6-only 强制单栈时，LookupIPv6WithResolver 才会被
        ErrIPv6Disabled 拦掉。
        """
        cell = cell.strip().strip('"').strip("'")
        if not cell:
            return None
        cands = [cell]
        if cell.count(":") == 1:
            cands.append(cell.rsplit(":", 1)[0])          # 裸 IPv4:port
        if cell.startswith("[") and "]" in cell:
            cands.append(cell[1:cell.index("]")])         # [IPv6]:port
        for c in cands:
            try:
                ip = ipaddress.ip_address(c)
            except ValueError:
                continue
            if not ip.is_global:
                continue
            return str(ip)
        return None


    # 兼容 CloudflareSpeedTest 的 result.csv（首列是 IP，带表头）
    # 与纯文本 IP 列表。逐个单元格解析，只认真正可路由的公网 IP，
    # 避免把「平均延迟 / 下载速度」这类数字误判成 IP。
    ips, seen = [], set()
    for line in raw.splitlines():
        for cell in line.split(","):
            got = parse_cell(cell)
            if got:
                if got not in seen:
                    seen.add(got)
                    ips.append(got)
                break          # 一行只取第一个合法 IP
        if len(ips) >= 200:
            break

    if not ips:
        print("cfopt: 响应里没有可用的公网 IP（IPv4 / IPv6 都收）")
        sys.exit(2)

    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(ips) + "\n")
    os.replace(tmp, dst)
    n4 = sum(1 for x in ips if ":" not in x)
    print("cfopt: 拉取到 %d 个优选 IP（IPv4 %d / IPv6 %d），最快的前几个：%s"
          % (len(ips), n4, len(ips) - n4, ", ".join(ips[:4])))

# ══ measure：内置测速，从 Cloudflare 网段抽样自测，产出与 fetch 同格式 ══
def _measure():
    import ipaddress, os, random, socket, ssl, sys, time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cidrs_arg, dst, sni = sys.argv[1], sys.argv[2], sys.argv[3]
    total = max(1, min(5000, int(sys.argv[4])))
    conc = max(1, min(256, int(sys.argv[5])))
    timeout = max(0.2, min(10.0, int(sys.argv[6]) / 1000.0))
    do_speed = sys.argv[7].lower() == "true"
    history, keep = sys.argv[8], max(1, int(sys.argv[9]))

    nets = []
    for part in cidrs_arg.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            print("cfopt measure: 跳过非法网段 %r" % part)
    if not nets:
        print("cfopt measure: 没有可用的候选网段")
        sys.exit(2)

    # 按【网段个数】均分抽样名额，而不是按地址空间大小 —— 否则 IPv6 的 /32
    # 天文数字般的地址空间会把 IPv4 挤到只剩每条网段 2 个样本。
    per_net = max(2, total // len(nets))
    cands = []
    for n in nets:
        base = int(n.network_address)
        if n.version == 4:
            span = max(0, n.num_addresses - 2)
            for _ in range(per_net):
                cands.append(str(ipaddress.ip_address(base + (random.randint(1, span) if span else 0))))
        else:
            for _ in range(per_net):
                cands.append(str(ipaddress.ip_address(base + random.randrange(n.num_addresses))))
    cands = list(dict.fromkeys(cands))[:total]


    def probe(ip):
        t0 = time.perf_counter()
        with socket.create_connection((ip, 443), timeout=timeout):
            return round((time.perf_counter() - t0) * 1000.0, 1), ip


    alive = []
    with ThreadPoolExecutor(max_workers=conc) as pool:
        futs = [pool.submit(probe, ip) for ip in cands]
        for fu in as_completed(futs):
            try:
                alive.append(fu.result())
            except Exception:
                continue
    alive.sort()

    if not alive:
        print("cfopt measure: %d 个候选全部不可达，沿用上次的 IP 列表" % len(cands))
        sys.exit(2)


    def speed_probe(ip):
        """带正确 SNI 的下载测速；返回 bytes/s。"""
        ctx = ssl.create_default_context()
        req = ("GET /__down?bytes=2000000 HTTP/1.1\r\nHost: %s\r\n"
               "User-Agent: clash.meta\r\nAccept: */*\r\nConnection: close\r\n\r\n" % sni)
        t0 = time.perf_counter()
        with socket.create_connection((ip, 443), timeout=5.0) as raw:
            with ctx.wrap_socket(raw, server_hostname=sni) as s:
                s.sendall(req.encode())
                got = 0
                while True:
                    b = s.recv(65536)
                    if not b:
                        break
                    got += len(b)
        dt = time.perf_counter() - t0
        return got / dt if dt > 0 else 0.0


    if do_speed:
        short = alive[:20]
        rated = []
        with ThreadPoolExecutor(max_workers=min(conc, 8)) as pool:
            futs = {pool.submit(speed_probe, ip): (ms, ip) for ms, ip in short}
            for fu in as_completed(futs):
                ms, ip = futs[fu]
                try:
                    bps = fu.result()
                except Exception:
                    bps = 0.0
                rated.append((bps, ms, ip))
        rated.sort(key=lambda x: (-x[0], x[1]))
        alive = [(ms, ip) for _, ms, ip in rated] + alive[len(short):]

    # 每个协议族各取前 keep 个：DIRECT 双栈是把 v4/v6 各成一组并发竞速，
    # 两组都要有代表；只会单栈的路径会自然忽略另一族。
    v4 = [ip for _, ip in alive if ":" not in ip][:keep]
    v6 = [ip for _, ip in alive if ":" in ip][:keep]
    best = v4 + v6
    if not best:
        print("cfopt measure: 没有可用的公网 IP")
        sys.exit(2)

    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(best) + "\n")
    os.replace(tmp, dst)

    detail = ", ".join("%s %sms" % (ip, ms) for ms, ip in alive[:4])
    try:
        with open(history, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("%s 候选=%d 可达=%d 采用=%d(IPv4 %d/IPv6 %d) 最快: %s\n"
                     % (time.strftime("%F %T"), len(cands), len(alive),
                        len(best), len(v4), len(v6), detail))
    except OSError:
        pass
    print("cfopt: 内置测速从 %d 个候选里选中 %d 个（IPv4 %d / IPv6 %d），最快: %s"
          % (len(cands), len(best), len(v4), len(v6), detail))

# ══ map：为「每个域名」单独挑最快的 IP ═══════════════════════════════
def _map():
    """按域名分别实测候选 IP，产出 cfopt-map.txt（域名→该域名最快的 IP）。

    为什么必须按域名选：Cloudflare 的限速是按「IP + 站点」计的，同一个 IP 对不同
    域名表现可以差一个数量级（实测 104.20.17.84 对 hdtime.org 的 TLS 只要 0.28s，
    对 ourbits.club 却要 3.95s）。用一份全局 IP 列表喂所有域名，必然有一半站点吃亏。

    判据用 **TLS 握手 + 首字节**（不是裸 TCP 握手）：慢在 TLS 的 IP 恰恰是握手最快
    的那种（SYN 小包快、证书传输被限速），只测握手会把它们当作优选。

    用法：map <ipfile> <domains> <out> <top> <conc>
    退出码：0=完成（部分域名失败也算完成，失败的域名会回退到全局列表）/ 1=参数或 IO 错误。
    """
    import os
    import re
    import socket
    import ssl
    import sys
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if len(sys.argv) < 5:
        print("cfopt: 用法: cfopt.py map <ipfile> <domains> <out> <top> <conc>")
        return 1
    ipfile, domains_arg, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        top = max(1, int(sys.argv[4]))
    except (IndexError, ValueError):
        top = 2
    try:
        conc = max(1, int(sys.argv[5])) if len(sys.argv) > 5 else 16
    except ValueError:
        conc = 16

    def load_ips():
        out, seen = [], set()
        try:
            with open(ipfile, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or line in seen:
                        continue
                    seen.add(line)
                    out.append(line)
        except OSError:
            pass
        return out

    def load_domains():
        # 与 inject 侧同一口径的域名合法性校验（那边是局部常量，这边得自己定义）
        domain_re = re.compile(
            r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)+$")
        out = []
        for part in re.split(r"[,\s;]+", domains_arg or ""):
            d = part.strip().lower().lstrip(".").rstrip(".")
            if not d:
                continue
            if d.startswith(("+", "*")):
                d = d[1:].lstrip(".")
            if d and domain_re.match(d) and d not in out:
                out.append(d)
        return out

    ips = load_ips()
    domains = load_domains()
    print("cfopt: 按域名选 IP —— 候选 IP %d 个，域名 %d 个" % (len(ips), len(domains)))
    if not ips or not domains:
        # 没有候选 IP 或没有域名都不算错误：产出一份空映射，inject 会回退到全局列表。
        print("cfopt: %s，跳过按域名选 IP（全部域名回退全局列表）"
              % ("没有候选 IP" if not ips else "没有域名"))
        try:
            with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("")
        except OSError as exc:
            print("cfopt: 写映射失败:", exc)
            return 1
        return 0

    # ── 复用：域名集合未变、映射文件足够新 → 跳过整轮实测 ──────────────
    # 为什么必须做：`cmd/main start` 会串行调用两次 cfopt_apply（cached + full），
    # 而 map 的耗时是 域名数 × 候选 IP 数 的 TLS 实测（实测 272 域名一轮 ≈ 227s）。
    # 结果就是每次重启都要等好几分钟、期间代理不可用。映射本身在几小时内几乎不变，
    # 所以给它一个 TTL + 域名指纹：一致就复用，域名增删或过期才重测。
    # 想强制重测：CFOPT_MAP_FORCE=1（或把 TTL 设 0）。
    import hashlib
    sig = hashlib.sha1(",".join(domains).encode("utf-8")).hexdigest()[:16]
    try:
        ttl_hours = float(os.environ.get("CFOPT_MAP_TTL", "12"))
    except ValueError:
        ttl_hours = 12.0
    if ttl_hours > 0 and not os.environ.get("CFOPT_MAP_FORCE"):
        try:
            with open(out_path, encoding="utf-8", errors="replace") as fh:
                head = fh.readline().strip()
            age = time.time() - os.path.getmtime(out_path)
            if head == "# map-sig: %s" % sig and age < ttl_hours * 3600:
                print("cfopt: 复用按域名映射（%.1f 小时前、域名指纹一致，TTL %.0fh）"
                      % (age / 3600.0, ttl_hours))
                return 0
        except OSError:
            pass

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE      # 只测连通与速度，不校验证书（各站脚本会自己校验）

    def probe(host, ip):
        """返回 (tls_ms, ttfb_ms) 或 None（TLS 直接失败）。"""
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        try:
            t0 = time.perf_counter()
            raw = socket.socket(family, socket.SOCK_STREAM)
            raw.settimeout(4.0)
            raw.connect((ip, 443))
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                tls_ms = (time.perf_counter() - t0) * 1000
                s.settimeout(5.0)
                s.sendall(("GET / HTTP/1.1\r\nHost: %s\r\nUser-Agent: Mozilla/5.0\r\n"
                           "Accept: */*\r\nConnection: close\r\n\r\n" % host).encode())
                t1 = time.perf_counter()
                first = s.recv(1)
                ttfb_ms = (time.perf_counter() - t1) * 1000
                if not first:
                    return None              # 连上了但一个字节都不回，同样不可用
                return tls_ms, ttfb_ms
        except (OSError, ssl.SSLError, ValueError):
            return None

    def pick(host):
        """并发探测该域名 × 全部候选，返回 (host, [(ip, 分数)] 升序)。"""
        scored = []
        with ThreadPoolExecutor(max_workers=max(1, min(conc, len(ips)))) as pool:
            futs = {pool.submit(probe, host, ip): ip for ip in ips}
            for fu in as_completed(futs):
                ip = futs[fu]
                got = fu.result()
                if got is None:
                    continue
                tls_ms, ttfb_ms = got
                # 分数：TLS 与首字节各占一半 —— 前者体现链路/证书，后者体现站点响应
                scored.append((0.5 * tls_ms + 0.5 * ttfb_ms, ip))
        scored.sort()
        return host, scored

    lines, ok = [], 0
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(domains)))) as pool:
        futs = {pool.submit(pick, h): h for h in domains}
        for fu in as_completed(futs):
            host, scored = fu.result()
            if not scored:
                # 严格语义：没有可验证的 IP 就**不留条目**，inject 会跳过该域名并保留
                # 订阅原有策略（通常走代理）。绝不把全局列表硬塞给它 —— 实测那样会让
                # 站点 TLS 直接失败（2026-11-13：272 域名时 hdwing/ourbits/1ptba 全挂）。
                print("cfopt: %-34s 全部 %d 个候选都建不起 TLS → %s（保留原策略）"
                      % (host, len(ips), "跳过该域名"))
                continue
            best = scored[:top]
            ip_list = " ".join(ip for score, ip in best)
            lines.append("%s\t%s" % (host, ip_list))
            ok += 1
            print("cfopt: %-34s %s（最优 %dms，共 %d 个可用）"
                  % (host, ip_list, int(best[0][0]), len(scored)))

    lines.sort()
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            # 指纹写在首行：judge 是否有变化的唯一依据，inject 侧会跳过 # 注释行
            fh.write("# map-sig: %s\n" % sig)
            fh.write("".join(l + "\n" for l in lines))
    except OSError as exc:
        print("cfopt: 写映射失败:", exc)
        return 1
    print("cfopt: 按域名选 IP 完成 —— %d/%d 个域名有专属映射，其余不注入（保留订阅策略）"
          % (ok, len(domains)))
    return 0


# ══ inject：把优选 IP 写进配置副本（hosts 映射 + 置顶 DIRECT 规则）═══
def _inject():
    import ipaddress, os, re, sys, time

    cfg_path = sys.argv[1]
    ipfile, domains_arg = sys.argv[2], sys.argv[3]
    snippet_path = sys.argv[5]
    try:
        top = max(1, min(16, int(sys.argv[4])))
    except ValueError:
        top = 4

    # 标记的唯一真源在 bash 侧（CFOPT_MARK），这里只保留同值兜底以便单独跑这一段。
    # 绝不要在这里改成与 bash 不同的值：它同时是「剥离上一轮注入」的判据。
    MARK = os.environ.get("CFOPT_MARK") or "# cfopt:"
    started = time.time()      # 本次运行起点，用于判断片段文件是不是「并发刚写出来的」
    DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)+$")


    def find_top(lines, key):
        """定位顶层键（必须顶格），返回行号；找不到返回 -1。

        键名可能被引号包起来（`"hosts":` 是合法 YAML，生成器常这么写），
        所以要允许可选引号 —— 否则会漏检、再追加一个同键块，造成重复键。
        """
        pat = re.compile(r"^[\"']?%s[\"']?\s*:" % re.escape(key))
        for i, ln in enumerate(lines):
            if pat.match(ln):
                return i
        return -1


    def block_range(lines, hi):
        """返回顶层键 hi 的子行区间 [hi+1, last]，跳过空行。"""
        j, last = hi + 1, hi
        while j < len(lines):
            s = lines[j]
            if s.strip() == "":
                j += 1
                continue
            if s[:1] in (" ", "\t"):
                last = j
                j += 1
                continue
            break
        return last


    def child_indent(lines, hi):
        """探测顶层键 hi 下第一条【真正内容行】的缩进；探测不到就退回两空格。

        不能写死 2 空格：订阅里 hosts 块用 4 空格缩进时，写死会产出缩进错乱的
        YAML（mihomo -t 直接拒掉，功能静默失效）。
        注释行不参与缩进判定（YAML 里注释可以与内容缩进不同），必须跳过，
        否则 `hosts:` 之后先有更浅的注释时会取到注释的缩进，
        结果同一个映射出现两种缩进 → 非法 YAML。
        """
        if hi < 0:
            return "  "
        j = hi + 1
        while j < len(lines):
            s = lines[j]
            if s.strip() == "" or s.lstrip().startswith("#"):
                j += 1
                continue
            if s[:1] in (" ", "\t"):
                m = re.match(r"^(\s*)", s)
                return m.group(1) if m else "  "
            break                      # 列 0 的真键 → 本块是空的
        return "  "


    def _flow_marks(s):
        """返回 (顶层逗号位置列表, 顶层冒号位置列表, 行尾注释起始位置或 -1)。

        顶层 = 既不在引号内、也不在 [] / {} 内。必须跟踪引号状态：
        `hosts: {a: "1.2.3.4,5.6.7.8"}` 若在引号内的逗号处切开，会拼成两行
        `"1.2.3.4` / `5.6.7.8"` —— 那样 YAML 仍然合法、mihomo -t 也照样通过，
        但用户条目已被静默改坏（这是最隐蔽的一类失败）。
        注释一并识别：YAML 里 `#` 只有位于行首、或前面是空白时才是注释，
        引号内的 `#` 不算。
        YAML 流式标量：单引号里 `''` 表示一个 `'`，双引号里 `\\` 是转义。
        """
        commas, colons, comment = [], [], -1
        depth, quote, i, n = 0, "", 0, len(s)
        while i < n:
            ch = s[i]
            if quote:
                if quote == "'" and ch == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        i += 2
                        continue
                    quote = ""
                elif quote == '"' and ch == "\\":
                    i += 2
                    continue
                elif quote == '"' and ch == '"':
                    quote = ""
                i += 1
                continue
            if ch in ("'", '"'):
                quote = ch
            # `#` 只有在行首、前面是空白、或前面是一个已结束的 token（`} ] ) " '`）
            # 时才是注释。`hosts: {a: 1.2.3.4}#c` 是合法写法，不能因为不认它就整次放弃。
            elif ch == "#" and (i == 0 or s[i - 1] in (" ", "\t", "}", "]", ")", "'", '"')):
                comment = i
                break
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "," and depth == 0:
                commas.append(i)
            elif ch == ":" and depth == 0 and s[i + 1:i + 2] in ("", " ", "\t"):
                colons.append(i)
            i += 1
        return commas, colons, comment


    def split_flow(s):
        """按【顶层、引号外】的逗号切分流式内容。"""
        commas, _, _ = _flow_marks(s)
        out, prev = [], 0
        for i in commas:
            out.append(s[prev:i])
            prev = i + 1
        if s[prev:].strip():
            out.append(s[prev:])
        return out


    def unquote_key(s):
        """YAML 键去引号：**只剥一层成对引号**，并按 YAML 规则还原转义。

        不能用 str.strip("'\\"")：它会把首尾**所有**引号字符都剥掉，于是「键本身
        就是引号字符」的 `"'": 2` 会被剥成空串，与库的判读矛盾 —— 库门禁会因此
        变成行为开关（tests/cfopt_test.py 的 C/D 两组正是在抓这个）。
        """
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            inner = s[1:-1]
            if s[0] == "'":
                return inner.replace("''", "'")
            return inner.replace('\\"', '"')
        return s


    def flow_key(piece):
        """取出 `键: 值` 里的键（去一层引号、小写）；取不到返回空串。"""
        _, colons, _ = _flow_marks(piece)
        if not colons:
            return ""
        return unquote_key(piece[:colons[0]]).lower()


    def strip_comment(s):
        """剥掉行尾注释；引号感知，别用裸 split("#")（`{a: "x # y"}` 会被切坏）。"""
        _, _, comment = _flow_marks(s)
        return s[:comment].rstrip() if comment >= 0 else s


    def flow_gate(inner, kind):
        """流式片段的「库级门禁」：库的判读与我们的手写切分不一致时，整次放弃。

        为什么这么设计（而不是直接用库去解析并产出）：
          * 写出内容仍由**已被 6 轮对抗式复核过**的手写路径产生，库只做"这俩对得上吗"；
          * 因此门禁的方向是**保守单向**的：库只可能让我们【多放弃】，绝不会让我们
            写错，也绝不会改变"能写时写什么"；
          * 于是「有库 / 无库」两条路径在既有语料上的产物必然逐字节一致
            （tests/cfopt_test.py 的 D 组用例正是在守这条不变量）。

        返回 True  = 可以按手写切分继续（库不可用、或库看不懂这段时也是 True，
                     即维持本文件重构前的既有行为，最终仍由 `mihomo -t` 裁决）；
        返回 False = 库判读与手写切分矛盾 → 调用方按"写法不支持"整次放弃。
        """
        if _yaml is None:
            return True
        text = ("{" + inner + "}") if kind == "map" else ("[" + inner + "]")
        try:
            data = _yaml.safe_load(text)
        except Exception:
            return True                 # 库看不懂 → 不改变既有行为
        pieces = [p for p in split_flow(inner) if p.strip()]
        if kind == "map":
            if not isinstance(data, dict) or len(data) != len(pieces):
                return False
            if not all(isinstance(k, str) for k in data):
                # 库把键**隐式定型**了：`yes:` → True、`null:`/`~:` → None、`0x10:` → 16。
                # 此时文本侧拿到的是原字样，两侧根本不可比 —— 判矛盾就会让「装了 PyYAML 的
                # 机器静默不注入、精简底座的机器却正常」。所以回退到不判（保持手写行为）。
                return True
            keys = [flow_key(p) for p in pieces]
            if any(k == "" for k in keys):
                return True     # 我们的键提取不可靠 → 不判矛盾，绝不让库成为行为开关
            return [str(k).lower() for k in data] == keys
        if not isinstance(data, list) or len(data) != len(pieces):
            return False
        return True


    def prune(body, emit_keys):
        """只删掉「本次注入会重复写出」的同名 hosts 键（含其数组延续行）。

        早先这里还会连带删掉同基名的兄弟域（注入 +.linux.do 时把用户自己写的
        sub.linux.do 一起删了），等于静默丢弃用户/订阅的数据。现收窄为仅同名键：
        重叠但不重名的条目一律保留，交给 mihomo 的 trie 按最具体匹配去裁决。
        """
        out, i, dropped = [], 0, 0
        while i < len(body):
            ln = body[i]
            m = re.match(r"^(\s+)([^\s#][^:]*?)\s*:", ln)
            if not m:
                out.append(ln)
                i += 1
                continue
            indent = len(m.group(1))
            k = unquote_key(m.group(2)).lower()
            if k in emit_keys:
                i += 1
                while i < len(body) and (
                    body[i].strip() == ""
                    or (len(body[i]) - len(body[i].lstrip())) > indent
                ):
                    i += 1
                dropped += 1
                continue
            out.append(ln)
            i += 1
        if dropped:
            print("cfopt: 合并 %d 个同名 hosts 键（避免重复键；其它条目一律保留）" % dropped)
        return out


    # 读取优选 IP：按协议族分组，每组各取前 top 个。
    # 同一个 hosts 键下 v4 与 v6 可以混放（config.parseHosts → utils.ToStringSlice
    # → resolver.NewHostValue，>1 个值时逐个 ParseAddr 收集，不区分协议族）。
    ips4, ips6, seen = [], [], set()
    try:
        with open(ipfile, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or line in seen:
                    continue
                try:
                    ip = ipaddress.ip_address(line)
                except ValueError:
                    continue
                if not ip.is_global:
                    continue
                seen.add(line)
                (ips4 if ip.version == 4 else ips6).append(line)
    except OSError:
        pass
    ips4, ips6 = ips4[:top], ips6[:top]
    ips = ips4 + ips6

    # 可选的按域名映射（<ipfile>.map 或环境变量 CFOPT_MAP 指定）：
    # 每行 `域名<TAB>ip1 ip2 ...`，仅接受公网地址，避免被污染的文件写进内网/回环地址。
    dom_ips = {}
    map_path = os.environ.get("CFOPT_MAP") or (ipfile + ".map")
    try:
        with open(map_path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "\t" not in line:
                    continue
                dom, rest = line.split("\t", 1)
                dom = dom.strip().lower().lstrip(".").rstrip(".")
                picked = []
                for cell in rest.split():
                    try:
                        ip_obj = ipaddress.ip_address(cell)
                    except ValueError:
                        continue
                    if ip_obj.is_global:
                        picked.append(cell)
                if dom and picked:
                    dom_ips[dom] = picked[:top]
    except OSError:
        pass                # 没有映射文件是正常情况（老部署 / 未开启按域名选 IP）
    if dom_ips:
        print("cfopt: 使用按域名映射 —— %d 个域名有专属 IP" % len(dom_ips))

    # 严格语义：**只要存在映射文件**，就只为「有可用映射」的域名注入；其余一律跳过，
    # 保留订阅原有策略（通常走代理）。否则 map 选不出 IP 的域名会被硬塞全局 IP，
    # 实测那会让站点 TLS 直接失败（2026-11-13：272 域名时 hdwing/ourbits/1ptba 全挂）。
    # 没有映射文件时仍按旧行为走全局列表（老部署 / 未开启按域名选 IP）。
    map_strict = os.path.exists(map_path)

    # 解析域名：example.com → 域名及所有子域；*.example.com → 仅一级子域
    domains = []
    for part in re.split(r"[,\s;]+", domains_arg or ""):
        d = part.strip().lower().lstrip(".").rstrip(".")
        if not d:
            continue
        if d.startswith("*."):
            key, base = d, d[2:]
        elif d.startswith("+."):
            key, base = "+." + d[2:], d[2:]
        else:
            key, base = "+." + d, d
        if not DOMAIN_RE.match(base):
            print("cfopt: 跳过非法域名 %r" % part)
            continue
        if all(key != k for k, _ in domains):
            domains.append((key, base))
    bases = sorted({b for _, b in domains})
    # 会被本次注入写出的键（去引号小写），只用来识别「同名重复键」
    emit_keys = {unquote_key(k).lower() for k, _ in domains}

    # 每个域名最终用哪些 IP。严格模式下，没有映射的域名不注入（见 map_strict 说明）。
    chosen, unmapped = [], []
    for key, base in domains:
        picked = dom_ips.get(base)
        if map_strict and not picked:
            unmapped.append(base)
            continue
        chosen.append((key, base, picked or ips))
    injected_bases = sorted({b for _, b, _ in chosen})
    if unmapped:
        head = ", ".join(unmapped[:8]) + ("…" if len(unmapped) > 8 else "")
        print("cfopt: 严格模式：%d 个域名没有可用映射，跳过注入（保留订阅策略）：%s"
              % (len(unmapped), head))
    if domains and not chosen:
        print("cfopt: 没有任何域名拿到可用映射，保持现有配置不变")
        sys.exit(0)

    if domains and not ips:
        print("cfopt: 配了域名但没有可用的优选 IP，保持现有配置不变")
        sys.exit(0)

    # ⚠️ 用**二进制**读取再自行解码与归一换行。两个坑：
    #   1) Python 文本层会把 `\r\r\n` 拆成两个换行（`\r\n` 一个 + 残余 `\r` 一个），
    #      于是肉眼正常的文件多出空行、注入位置错乱 —— 所以必须绕开文本层；
    #   2) 归一顺序要让「任意个 `\r` 紧跟 `\n`」只算**一个**换行，
    #      同时不能动真正的空行（`\n\n` 与 CRLF 文件里的 `\r\n\r\n` 都要保留）。
    #      先 `\r+\n`→`\n` 再收尾单个 `\r`，两种情况都正确。
    with open(cfg_path, "rb") as fh:
        raw_text = fh.read().decode("utf-8", "replace")
    lines = re.sub(r"\r+\n", "\n", raw_text).replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    # 先剔除上一轮的注入，保证幂等
    stripped = [ln for ln in lines if MARK not in ln]
    had_marks = len(stripped) != len(lines)
    lines = stripped

    host_count = rule_count = 0
    wrote_snippet = False
    if domains and ips:
        # ---------- 第一步：只做判定，先不动 lines ----------
        hi = find_top(lines, "hosts")
        ind = child_indent(lines, hi)      # 跟随已有 hosts 块的缩进（探测不到才用两空格）
        entries = []
        # 按域名的专属映射（map 子命令产出，可选）：有就给该域名用自己实测最快的 IP。
        # 严格模式下（存在映射文件）没有映射的域名已经在 chosen 里被剔除 —— 不再回退全局，
        # 因为那正是「站点被塞进打不通的 IP」的事故来源。
        for key, base, ips_for in chosen:
            entries.append("%s'%s': %shosts" % (ind, key, MARK))
            for ip in ips_for:
                entries.append("%s- %s %shosts" % (ind + "  ", ip, MARK))
        hosts_flow = None
        if hi < 0:
            host_form = "append"
        else:
            # 先剥行尾注释（引号感知）：`hosts: {a: 1.2.3.4}   # 注释` 是合法 YAML，
            # 不剥就会落到「写法不支持」分支，让整个功能失效。
            rest = strip_comment(lines[hi].split(":", 1)[1].strip())
            if rest == "" or rest.startswith("#"):
                # 块式：还要确认子行是【映射】而不是顶格序列（`hosts:` 后直接列 0 的 `- x`）。
                # 后者 mihomo 本来也加载不了（Hosts 是 map），但插进去会产出非法 YAML，
                # 被 -t 拒掉，不如直接判为不支持、整次放弃。
                k = hi + 1
                while k < len(lines) and (lines[k].strip() == "" or
                                          lines[k].lstrip().startswith("#")):
                    k += 1
                host_form = None if (k < len(lines) and lines[k][:1] == "-") else "block"
            elif rest in ("{}", "null", "~"):
                host_form = "empty"
            elif rest.startswith("{") and rest.endswith("}"):
                if flow_gate(rest[1:-1], "map"):
                    host_form, hosts_flow = "flow", rest[1:-1]
                else:
                    host_form = None       # 库判读与手写切分矛盾 → 整次放弃（更保守）
            else:
                host_form = None           # `hosts: 10.0.0.1` 这类内核不接受的标量

        ri = find_top(lines, "rules")
        indent = "  "
        rules_map = False
        if ri >= 0:
            # 跳过空行与注释，再找第一条真正的列表项。
            # 顶格序列（`rules:` 之后直接是列 0 的 `- x`，YAML 允许）也必须认出来，
            # 否则会用两空格注入，与既有列 0 条目混排 → 非法 YAML。
            # 反过来，有缩进却不是序列项（`rules:` 下写 `a: 1`）说明它是个块映射，
            # 往映射里插序列项同样产出非法 YAML —— 一并判为不支持。
            j = ri + 1
            while j < len(lines):
                s = lines[j]
                if s.strip() == "" or s.lstrip().startswith("#"):
                    j += 1
                    continue
                m = re.match(r"^(\s*)-", s)
                if m:
                    indent = m.group(1)
                    break
                if s[:1] not in (" ", "\t"):
                    break                   # 已是下一个顶层键，rules 块到此结束
                rules_map = True
                break
        # 规则只给「真的写出了 hosts」的域名 —— 严格模式跳过的那批不该被强制直连，
        # 否则等于把域名从走代理反转成直连，比不注入更糟。
        rule_lines = ["%s- 'DOMAIN-SUFFIX,%s,DIRECT' %srule" % (indent, b, MARK)
                      for b in injected_bases]
        rrest = strip_comment(lines[ri].split(":", 1)[1].strip()) if ri >= 0 else ""
        rules_flow = None
        if ri < 0:
            # 配置里根本没有 rules 键。我们生成/订阅写入的配置一定有（见 fetch_subscription
            # 的模板 `rules: - MATCH,DIRECT`），所以这只可能是手写配置 —— 此时凭空补一条
            # MATCH,DIRECT 会把本该走代理的流量静默改成**全直连**，比不注入更糟，而我们
            # 又无从知道用户想要的兜底策略。因此与其它「写法不支持」一致：整次放弃。
            rule_form = None
        elif rrest.startswith(("[]", "null", "~")):
            rule_form = "replace"
        elif rrest == "":
            rule_form = None if rules_map else "insert"
        elif rrest.startswith("[") and rrest.endswith("]"):
            if flow_gate(rrest[1:-1], "seq"):
                rule_form, rules_flow = "flow", rrest[1:-1]
            else:
                rule_form = None           # 库判读与手写切分矛盾 → 整次放弃（更保守）
        else:
            rule_form = None

        # ---------- 第二步：两边都能写才动手 ----------
        # 只写一边都会静默失效：只写 hosts 而没有置顶 DIRECT，这些域名仍按订阅策略
        # 走代理、由代理端远端解析，hosts 根本用不上（改了配置却毫无效果）；只插
        # DIRECT 而没有 hosts 映射，则把这些域名从走代理反转成直连（比不注入更糟）。
        if host_form is None or rule_form is None:
            if rule_form is None and ri < 0:
                print("cfopt: 配置里没有 rules 键：不凭空补 MATCH,DIRECT —— 那会把本该走代理的"
                      "流量静默改成全直连，比不注入更糟")
            print("cfopt: %s 写法不支持，本次不注入任何内容"
                  % ("hosts" if host_form is None else "rules"))
        else:
            if host_form == "append":
                lines.append("hosts:")
                lines.extend(entries)
            elif host_form == "block":
                last = block_range(lines, hi)
                body = prune(lines[hi + 1:last + 1], emit_keys)
                lines[hi + 1:last + 1] = body + entries
            elif host_form == "empty":
                lines[hi] = "hosts:"
                lines[hi + 1:hi + 1] = entries
            else:                                   # flow
                converted = ["hosts:"]
                for piece in split_flow(hosts_flow):
                    # 合并同名键，否则写出重复键 → 校验失败 → 整次注入被放弃
                    if not piece.strip() or flow_key(piece) in emit_keys:
                        continue
                    converted.append(ind + piece.strip())
                converted.extend(entries)
                lines[hi:hi + 1] = converted

            # hosts 的改动可能增减了行数，rules 的下标必须在改动之后重取
            ri = find_top(lines, "rules")
            if rule_form == "replace":
                lines[ri] = "rules:"
                lines[ri + 1:ri + 1] = rule_lines
            elif rule_form == "insert":
                lines[ri + 1:ri + 1] = rule_lines
            else:                                   # flow：转块式后把我们的规则插到最前
                converted = ["rules:"]
                converted.extend("%s- %s" % (indent, p.strip())
                                 for p in split_flow(rules_flow) if p.strip())
                converted[1:1] = rule_lines
                lines[ri:ri + 1] = converted

            host_count, rule_count = len(injected_bases), len(rule_lines)
            print("cfopt: 注入 %d 个域名 / %d 个 IP / %d 条 DIRECT 规则"
                  % (host_count, len(ips), rule_count))

            # 同步生成客户端 hosts 片段，覆盖「不走代理端口」的场景。
            # 只在【真正注入了 hosts】时才写，否则会生出一份与内核实际状态相反的
            # 文件，误导按 README 去用它的用户。属附赠能力，写失败不连累主机注入。
            try:
                with open(snippet_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("# 由 fnOS mihomo 应用生成 —— Cloudflare 优选 IP 直连片段\n")
                    fh.write("# 用法：追加到客户端 /etc/hosts，或作为局域网 DNS 的 hosts 重写\n")
                    fh.write("# 生成时间：%s\n" % time.strftime("%F %T"))
                    # 片段只写「真的注入了」的域名与它们各自的 IP，与内核状态严格一致
                    for _key, base, ips_for in chosen:
                        for ip in ips_for:
                            fh.write("%s %s   # cfopt\n" % (ip, base))
                wrote_snippet = True
            except OSError as exc:
                print("cfopt: 写入客户端 hosts 片段失败（不影响注入）：%s" % exc)
    elif had_marks:
        print("cfopt: 功能未启用，已清除上一次的注入")

    # 没写片段时，清掉上一轮留下的陈旧片段：功能已关闭、或本次注入被放弃的情况下，
    # 留着它会让按 README 去用它的用户拿到与内核实际状态相反的文件。
    # 只删【本次运行开始之前】就存在的：否则会误删另一个并发成功刷新刚写出的新片段。
    if not wrote_snippet:
        try:
            if os.path.getmtime(snippet_path) < started:
                os.remove(snippet_path)
        except OSError:
            pass

    # 只有内容真的变了才落盘：否则「整次放弃」也会把候选重写一遍，顺手把 CRLF / 缺尾换行
    # 规范化成 LF —— bash 的 `cmp -s` 就会判为"有变化"，于是走校验+换盘，日志却说
    # 「本次未注入任何内容」，而 README 承诺的是"一个字节都不改"。
    text = "\n".join(lines).rstrip("\n") + "\n"
    with open(cfg_path, "r", encoding="utf-8", newline="") as fh:
        cur = fh.read()
    # 判据只看“语义内容”：行尾风格（CRLF/LF）与**有没有尾换行**都算纯书写差异，
    # 不构成“需要落盘”。否则缺尾换行的配置在「整次放弃」时会被补上一个换行，
    # bash 的 `cmp -s` 判为有变化 → 白走一次校验+换盘，而日志说的是「未注入任何内容」。
    if cur.replace("\r\n", "\n").rstrip("\n") != text.rstrip("\n"):
        with open(cfg_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

# ══ scan：找出「哪些站点其实套了 Cloudflare」 ═══════════════════════
def _scan():
    """扫描候选域名，判定哪些落在 Cloudflare 官方网段，供用户挑选后填入域名清单。

    为什么需要它：本应用的优选注入靠顶层 `hosts`，而 hosts 只能按**字面域名**匹配，
    没有「匹配所有域名」的通配符（`'*'` 会把全网改成同一组 IP）。所以「凡是套 CF 的
    站点自动生效」在 hosts 机制下不可实现；能自动化的只有「找出哪些域名套了 CF」这一步。

    参数：scan <cfopt-hosts.txt> <out.txt> [<extra-domains>]
      1) cfopt-hosts.txt —— 当前已注入的域名（用于标注「已在清单中」）
      2) out.txt         —— 输出建议清单（每行：域名<TAB>v4数<TAB>v6数<TAB>状态）
      3) extra-domains   —— 额外要扫的域名，逗号分隔（可为空）

    退出码：0=正常（哪怕一个都没命中）/ 1=参数或 IO 错误。
    """
    import ipaddress, os, sys

    # Cloudflare 官方网段（https://api.cloudflare.com/client/v4/ips，2026-10-19 实取）。
    # 内置一份而不是每次联网拉取：注入链路上不能多一个网络依赖，且这份列表极少变动。
    CF_V4 = ["173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
             "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
             "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
             "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22"]
    CF_V6 = ["2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
             "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32"]
    CF_NETS = [ipaddress.ip_network(c) for c in CF_V4 + CF_V6]

    if len(sys.argv) < 3:
        print("cfopt: 用法: cfopt.py scan <已注入片段> <输出文件> [<额外域名>]")
        return 1
    snippet_path, out_path = sys.argv[1], sys.argv[2]
    extra = sys.argv[3] if len(sys.argv) > 3 else ""

    def is_cf(ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in CF_NETS)

    def parse_name(data, off):
        labels, jumped, orig = [], False, off
        while off < len(data):
            length = data[off]
            if length == 0:
                off += 1
                break
            if length & 0xC0 == 0xC0:
                if off + 1 >= len(data):
                    break
                ptr = ((length & 0x3F) << 8) | data[off + 1]
                if not jumped:
                    orig = off + 2
                jumped = True
                off = ptr
                continue
            labels.append(data[off + 1:off + 1 + length].decode("latin1", "replace"))
            off += 1 + length
        return ".".join(labels), (orig if jumped else off)

    def dns_query(name, qtype, server, timeout=5.0):
        """最小 DNS 客户端（纯标准库）。返回 IP 列表；失败返回 None。"""
        import socket, struct
        tid = b"\x5a\x5b"
        header = tid + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 0)
        try:
            qname = b"".join(bytes([len(p)]) + p.encode("idna")
                             for p in name.split(".") if p) + b"\x00"
        except UnicodeError:
            return None
        packet = header + qname + struct.pack("!HH", qtype, 1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, server)
            data, _ = sock.recvfrom(4096)
        except OSError:
            return None
        finally:
            sock.close()
        if len(data) < 12 or data[:2] != tid or (data[3] & 0x0F) != 0:
            return None
        qd, an = struct.unpack("!HH", data[4:8])
        off = 12
        for _ in range(qd):
            _, off = parse_name(data, off)
            off += 4
        out = []
        for _ in range(an):
            _, off = parse_name(data, off)
            if off + 10 > len(data):
                break
            rtype, _rc, _ttl, rdlen = struct.unpack("!HHIH", data[off:off + 10])
            off += 10
            rd = data[off:off + rdlen]
            off += rdlen
            if rtype == 1 and rdlen == 4:
                out.append(socket.inet_ntoa(rd))
            elif rtype == 28 and rdlen == 16:
                out.append(str(ipaddress.IPv6Address(rd)))
        return out

    # 收集要扫的域名：已注入的 + 用户额外给的
    existing = set()
    try:
        with open(snippet_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    existing.add(parts[1].lower().lstrip("."))
    except OSError:
        pass                      # 片段不存在是正常情况（还没注入过）

    cands = []
    seen = set()
    for raw in ([d for d in extra.split(",")] if extra else []):
        d = raw.strip().lower().lstrip(".").rstrip(".")
        if d and d not in seen:
            seen.add(d)
            cands.append(d)
    for d in sorted(existing):
        if d not in seen:
            seen.add(d)
            cands.append(d)

    if not cands:
        print("cfopt: 没有可扫描的域名（片段为空且未提供额外域名）")
        try:
            with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("# cfopt 扫描结果：无候选域名\n")
        except OSError as exc:
            print("cfopt: 写输出失败:", exc)
            return 1
        return 0

    # 上游 DNS：优先用系统解析器，避免依赖内核 DNS（fake-ip 下内核只会回 198.18.x.x）
    upstream = ("223.5.5.5", 53)
    lines = ["# cfopt 扫描结果：域名 <TAB> IPv4数 <TAB> IPv6数 <TAB> 结论"]
    hits = 0
    for name in cands:
        v4 = dns_query(name, 1, upstream) or []
        v6 = dns_query(name, 28, upstream) or []
        ips = v4 + v6
        cf_ips = [i for i in ips if is_cf(i)]
        if not ips:
            verdict = "解析失败"
        elif cf_ips:
            verdict = "套 CF" + ("（已在清单）" if name in existing else "")
            hits += 1
        else:
            verdict = "非 CF"
        lines.append("%s\t%d\t%d\t%s" % (name, len(v4), len(v6), verdict))
        print("cfopt: %-42s v4=%-2d v6=%-2d %s" % (name, len(v4), len(v6), verdict))

    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        print("cfopt: 写输出失败:", exc)
        return 1
    print("cfopt: 扫描完成，%d 个域名中 %d 个套 CF" % (len(cands), hits))
    return 0


# ══ domains：管理「走优选 IP 的域名」清单文件 ═════════════════════════
def _domains():
    """维护域名清单文件（每行一个域名，`#` 开头为注释）。

    为什么清单不放在「应用设置」里：飞牛向导的 text 字段有 1023 字节上限，而站点清单
    是**数据**不是设置（79 个域名就 929 字节，再多就存不下）；更糟的是表单提交是「整份
    替换」，浏览器自动填充的旧值会把整份清单冲掉（2026-10-19 真机踩过）。改成文件后
    既没有长度限制，也不会被一次保存误覆盖。

    用法：domains <file> list|add <a,b>|rm <a,b>|set <a,b>
    退出码：0=成功 / 1=参数或 IO 错误。
    """
    import os
    import re
    import sys

    if len(sys.argv) < 2:
        print("cfopt: 用法: cfopt.py domains <文件> list|add|rm|set [域名...]")
        return 1
    path, action = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "list"
    domain_re = re.compile(
        r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)+$")

    def norm(part):
        d = part.strip().lower().lstrip(".").rstrip(".")
        if d.startswith(("+", "*")):
            d = d[1:].lstrip(".")
        return d if d and domain_re.match(d) else None

    def load():
        out = []
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    d = norm(line)
                    if d and d not in out:
                        out.append(d)
        except OSError:
            pass
        return out

    def save(items):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 走 Cloudflare 优选 IP 的域名，每行一个（# 开头为注释）\n")
            fh.write("".join(d + "\n" for d in sorted(items)))
        os.replace(tmp, path)          # 原子替换：并发读的一方不会读到半截文件

    args = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
    wanted, bad = [], []
    for part in re.split(r"[,\s;]+", args):
        if not part.strip():
            continue
        d = norm(part)
        (wanted.append(d) if d else bad.append(part.strip()))

    if bad:
        print("cfopt: 跳过非法域名 %r" % bad[:5])
        if action in ("add", "set") and not wanted:
            return 1

    if action == "list":
        for d in load():
            print(d)
        return 0
    if action == "add":
        cur = load()
        added = [d for d in wanted if d not in cur]
        save(cur + added)
        print("cfopt: 新增 %d 个域名，当前共 %d 个" % (len(added), len(set(cur) | set(wanted))))
        return 0
    if action == "rm":
        cur = load()
        keep = [d for d in cur if d not in wanted]
        save(keep)
        print("cfopt: 移除 %d 个域名，当前共 %d 个" % (len(cur) - len(keep), len(keep)))
        return 0
    if action == "set":
        save(wanted)
        print("cfopt: 清单已设为 %d 个域名" % len(set(wanted)))
        return 0
    print("cfopt: 未知操作 %r（支持 list|add|rm|set）" % action)
    return 1


# ══ 派发 ═══════════════════════════════════════════════════════════
def main(argv):
    if not argv:
        print("cfopt: 用法: cfopt.py {fetch|measure|inject|scan|map} ...")
        return 1
    sub, rest = argv[0], argv[1:]
    # 让各段沿用抽取前的 sys.argv[N] 取参方式：argv[1] 就是第一个业务参数。
    sys.argv = [sys.argv[0]] + rest
    if sub == "fetch":
        return _fetch()
    if sub == "measure":
        return _measure()
    if sub == "inject":
        return _inject()
    if sub == "scan":
        return _scan()
    if sub == "map":
        return _map()
    if sub == "domains":
        return _domains()
    print("cfopt: 未知子命令 %r" % sub)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
