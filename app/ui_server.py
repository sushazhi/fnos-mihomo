#!/usr/bin/env python3
"""Mihomo fnOS 应用 — 内置状态页服务

只依赖 Python 标准库，因为 fnOS 自带 /usr/bin/python3。

职责：
  * 提供状态页静态文件（ui/ 目录）
  * 提供 /api/status 汇总内核状态，供状态页轮询
  * 白名单只读转发少量 Clash API 端点（密钥留在服务端）

支持两种访问模型（可同时启用）：
  * 端口服务：TCP :9092（fnOS 桌面入口 / 局域网直连）
  * 统一网关：Unix Socket ${TRIM_APPDEST}/app.sock（复用 NAS 登录态与域名）
"""
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UI_PORT = int(os.environ.get("MIHOMO_UI_PORT", "9092"))
API_PORT = int(os.environ.get("MIHOMO_API_PORT", "9090"))
UI_ROOT = os.environ.get("MIHOMO_UI_ROOT", os.path.join(os.path.dirname(__file__), "ui"))
LOG_FILE = os.environ.get("MIHOMO_LOG_FILE", "")
DATA_DIR = os.environ.get("MIHOMO_DATA_DIR", os.path.dirname(UI_ROOT))

API_BASE = f"http://127.0.0.1:{API_PORT}"

# 统一网关公开前缀，必须与 app/ui/config 的 gatewayPrefix 一致。
# 网关转发时保留完整前缀，应用需自行剥离（见 normalize_path）。
# 应用注册了多个入口（主入口 + 状态页），需按**最长前缀优先**匹配，
# 否则 /app/mihomo/status 会被 /app/mihomo 先剥成 /status 而 404。
# 按**长度倒序**排列，实现"最长前缀优先"匹配。
# ⚠️ 不能用 sorted() —— 那是字典序，会把 /app/mihomo 排在
# /app/mihomo/status 之前，导致子路径入口被短前缀错误剥离成 /status 而 404。
GATEWAY_PREFIXES = sorted(
    (p.strip() for p in os.environ.get(
        "MIHOMO_GATEWAY_PREFIXES", "/app/mihomo/status,/app/mihomo"
    ).split(",") if p.strip()),
    key=len,
    reverse=True,
)
# 兼容单前缀变量
GATEWAY_PREFIX = GATEWAY_PREFIXES[-1] if GATEWAY_PREFIXES else "/app/mihomo"

# "状态页"专用入口的前缀（app/ui/config 里的 mihomo.status）。
# 它与主入口共用同一个 Socket，剥掉前缀后内部路径都是 "/"，
# 所以只能拿**原始路径**判断请求来自哪个入口（见 is_status_entry）。
STATUS_PREFIXES = tuple(p for p in GATEWAY_PREFIXES if p.endswith("/status"))

# 允许匿名转发到 Clash API 的**只读**端点白名单。
#
# 状态页自身（/api/status）由服务端聚合数据后返回**计数与非敏感字段**，
# 并不需要把原始端点暴露给浏览器，因此这里保持最小集合。
#
# 明确**不得**加入（安全审查结论）：
#   /connections —— 暴露全部代理访问目标（隐私泄露）
#   /configs     —— 回显 secret 本身
#   /proxies     —— 暴露节点名与订阅信息
#   /rules       —— 暴露完整规则集
# 若将来确实需要，应改由 /api/status 服务端聚合后输出白名单字段，
# 而不是放开原始透传。
ALLOWED_PROXY_ENDPOINTS = {
    "/version",
}

# 两个内置面板的静态文件目录（构建时下载到 app/panels/）
PANELS_DIR = os.environ.get(
    "MIHOMO_PANELS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "panels"),
)

# 面板会以**同源根路径**请求 Clash API（如 /proxies、/version）。
# 只有在经由统一网关（已校验登录态）时才转发这些路径，
# 避免局域网匿名客户端借道访问敏感端点。
PROXYABLE_ROOT_PATHS = {
    "/version", "/configs", "/proxies", "/rules", "/connections",
    "/providers", "/rules", "/traffic", "/memory", "/logs", "/group",
    "/dns", "/cache", "/script", "/profile", "/restart", "/upgrade",
}

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

# 面板内「切换面板」浮动入口的脚本路径（文件放在 app/ui/panel-switch.js）。
#
# 为什么必须是**独立文件**、而不是内联进面板 HTML：
#   两个面板都注册了 Workbox Service Worker，且各自只注册了一条 NavigationRoute
#   （绑定到预缓存的 index.html）。NavigationRoute 只匹配导航请求
#   （request.mode === "navigate"），**不拦截** <script src> 这类子资源请求，
#   所以本文件永远从服务端取最新版：改切换逻辑不必重打面板 HTML，
#   更不用去动 sw.js 里的预缓存版本号。
#
#   （那个版本号一旦漏改，对老用户就是永久不可见 —— 实测踩过：
#    Zashboard 的 index.html 实为 76efd638…，sw.js 里却还写着 8696cf50…，
#    sw.js 字节没变 → 浏览器不更新 SW → 一直拿旧的 index.html。）
SWITCH_SCRIPT = "/panel-switch.js"


def read_secret() -> str:
    """从 settings.env 读取 API 密钥（不写入日志）。"""
    path = os.path.join(DATA_DIR, "settings.env")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MIHOMO_API_SECRET="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def api_get(path: str, timeout: float = 3.0):
    """调用 Clash API。返回 (status, parsed_body_or_None)。"""
    request = urllib.request.Request(API_BASE + path)
    secret = read_secret()
    if secret:
        request.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return 0, None


def detect_lan_ip() -> str:
    """探测本机局域网 IP。

    用途：状态页要提示「面板请连 http://<NAS-IP>:9090」。
    在统一网关下 location.hostname 是 NAS 的访问域名（可能是反代域名或
    HTTPS 域名），拿它拼 :9090 往往不可达；因此由**服务端**给出真实局域网 IP。
    """
    override = os.environ.get("MIHOMO_LAN_IP", "")
    if override:
        return override
    sock = None
    try:
        # 不实际发包，只让内核选出出口网卡对应的源地址
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        if sock is not None:
            sock.close()


LAN_IP = detect_lan_ip()


class Handler(BaseHTTPRequestHandler):
    server_version = "mihomo-fnos"

    def client_label(self) -> str:
        """安全地取客户端标识。

        ⚠️ 不能用 self.address_string()：它内部取 self.client_address[0]，
        而在 AF_UNIX（统一网关 Socket）下 client_address 是空字符串，
        会抛 IndexError 导致整个请求 500 / 连接被断开。
        另外网关转发时真实客户端是网关进程，可通过 X-Trim-Username 记录用户。
        """
        addr = self.client_address
        if isinstance(addr, tuple) and addr:
            return str(addr[0])
        # AF_UNIX：用网关注入的可信用户名，退化为 unix
        return self.headers.get("X-Trim-Username") or "unix"

    def log_message(self, fmt, *args):        # 收敛默认 stderr 噪音
        if LOG_FILE:
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as handle:
                    handle.write("[ui] %s - %s\n" % (self.client_label(), fmt % args))
            except OSError:
                pass

    # ── 响应助手 ────────────────────────────────────────────
    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status: int, text: str, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── 路由 ────────────────────────────────────────────────
    def normalize_path(self, path: str) -> str:
        """把统一网关前缀剥离成应用内部路径。

        网关按 gatewayPrefix 转发，**保留完整前缀**：
            客户端请求 /app/mihomo/api/status
            → 转发到 Socket 时仍是 /app/mihomo/api/status
        因此应用必须自己剥掉前缀，否则会 404。
        （官方文档提醒：「服务需要适配网关路由和鉴权」。）

        本应用注册了**两个**网关入口（主入口与状态页入口），
        二者共用同一个 Socket，因此要按"最长前缀优先"依次尝试剥离。

        端口服务模式下请求本来就是 /api/status，剥离是幂等的。
        """
        for prefix in GATEWAY_PREFIXES:
            if prefix and (path == prefix or path.startswith(prefix + "/")):
                stripped = path[len(prefix):]
                return stripped if stripped.startswith("/") else ("/" + stripped if stripped else "/")
        return path or "/"

    def is_via_gateway(self) -> bool:
        """是否经由统一网关（已通过 NAS 登录态校验）。

        网关会注入 X-Trim-Userid；端口直连则没有。
        完整 Clash API 反代（含写操作）**只对网关流量开放**，
        因为网关已强制校验登录态；局域网直连只能访问只读白名单。
        """
        return self.headers.get("X-Trim-Userid") is not None

    def is_status_entry(self, raw: str) -> bool:
        """判断这次请求走的是否是"状态页"网关入口。

        状态页入口 /app/mihomo/status 与主入口 /app/mihomo 共用同一个
        Socket，剥掉各自前缀后内部路径**都是 "/"**，仅看归一化后的 path
        无法区分二者。实测缺陷：从桌面点「Mihomo 状态」进来会显示成
        面板选择页，状态页只能靠 /app/mihomo/status/index.html 打开。
        因此必须回到**原始路径**（未剥前缀）上判断入口。
        """
        for prefix in STATUS_PREFIXES:
            if raw == prefix or raw.startswith(prefix + "/"):
                return True
        return False

    # 需要"目录语义"的路径：访问时若缺尾斜杠会导致相对链接解析错误。
    # 这些是会被浏览器当作目录基址的入口页。
    #
    # ⚠️ 这里**只**列 "/"，不要把 "/index.html" 放进来：
    #    /app/mihomo/index.html 的相对链接本来就以 /app/mihomo/ 为基址，是对的；
    #    把它 301 成 /app/mihomo/index.html/ 既多绕一跳，又把基址变成
    #    /app/mihomo/index.html/ —— 相对链接会解析成
    #    /app/mihomo/index.html/panel/...（404）。实测该路径只是"碰巧"还能
    #    落到状态页（realpath 会抹掉尾斜杠），属于不该依赖的巧合。
    DIR_LIKE_PATHS = ("/",)

    def needs_trailing_slash(self, raw: str, path: str) -> bool:
        """判断是否该把请求 301 到带尾斜杠的版本。

        浏览器的相对路径解析以**地址栏 URL** 为基准：
          地址 /app/mihomo    + href="panel/zashboard/"
          -> /app/panel/zashboard/     ← 404（实测线上报错）
          地址 /app/mihomo/   + href="panel/zashboard/"
          -> /app/mihomo/panel/zashboard/   ✅

        因此目录型入口必须带尾斜杠。
        """
        if not raw or raw.endswith("/"):
            return False
        if path in self.DIR_LIKE_PATHS:
            return True
        # 面板目录：/panel/<name>（不超过两段）
        if path.startswith("/panel/"):
            rest = path[len("/panel/"):]
            return bool(rest) and "/" not in rest
        return False

    def do_GET(self):
        raw = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        path = self.normalize_path(raw)

        if self.needs_trailing_slash(raw, path):
            target = raw + "/" + (("?" + query) if query else "")
            self.send_response(301)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/status":
            return self.handle_status()

        # ── 面板路由 ────────────────────────────────────────
        # "Mihomo 状态"桌面入口的 gatewayPrefix 是 /app/mihomo/status，
        # 按最长前缀剥掉后内部路径正好是 "/"，与主入口**无法区分**。
        # 因此必须先按**原始路径**判断这次请求走的是哪个网关入口，
        # 否则状态入口会显示成面板选择页（实测缺陷，状态页只能靠
        # /app/mihomo/status/index.html 才能打开）。
        if path in ("/", "/index.html") and self.is_status_entry(raw):
            return self.serve_static("/index.html")
        if path == "/":
            return self.serve_chooser()
        # ui/index.html 本身就是状态页文件，直接给文件。
        # 不再 301 到 /index.html/ —— 那会把相对链接的基址带偏（见 DIR_LIKE_PATHS）。
        if path == "/index.html":
            return self.serve_static("/index.html")
        # 面板内切换入口脚本：单独走一个方法，只为了强制 no-cache
        if path == SWITCH_SCRIPT:
            return self.serve_switcher()
        if path == "/panel/metacubexd" or path.startswith("/panel/metacubexd/"):
            return self.serve_panel("metacubexd", path[len("/panel/metacubexd"):])
        if path == "/panel/zashboard" or path.startswith("/panel/zashboard/"):
            return self.serve_panel("zashboard", path[len("/panel/zashboard"):])

        # ── Clash API 反代 ──────────────────────────────────
        # 面板需要完整 API（含 /proxies、/connections、/configs 等）。
        # 两条通道的授权级别不同：
        #   * 网关（已登录）→ 放行完整 API，面板因此无需填写密钥
        #   * 局域网直连     → 仅放行只读白名单，避免裸奔
        if path.startswith("/api/"):
            upstream = path[len("/api"):]
            if self.is_via_gateway():
                return self.proxy_api(upstream, query)
            if upstream not in ALLOWED_PROXY_ENDPOINTS:
                return self.send_json(403, {"error": "该端点未开放（局域网直连仅限只读）"})
            status, payload = api_get(upstream)
            if payload is None:
                return self.send_json(502, {"error": "Clash API 不可用"})
            return self.send_json(status or 200, payload)

        # 面板自身以同源方式请求 /proxies、/version 等（无 /api 前缀）。
        # ⚠️ 必须按**首段**匹配而非全等：面板会请求带子路径的端点，例如
        #   PUT /proxies/NODE-B/select        （切换节点）
        #   GET /proxies/NODE-B/delay         （测延迟）
        #   GET /providers/proxies/xxx
        # 全等匹配会让这些请求 404。
        if self.is_via_gateway() and self.is_proxyable_root(path):
            # WebSocket 升级（/traffic、/memory、/connections、/logs）
            # 必须走双向透传，否则面板「连上但图表不动」。
            if self.is_websocket_upgrade():
                return self.proxy_websocket(path, query)
            return self.proxy_api(path, query)

        return self.serve_static(path)

    @staticmethod
    def is_proxyable_root(path: str) -> bool:
        """判断无 /api 前缀的路径是否属于要反代给 Clash API 的端点。"""
        head = "/" + path.lstrip("/").split("/", 1)[0]
        return head in PROXYABLE_ROOT_PATHS

    # ── 面板 ────────────────────────────────────────────────
    def serve_chooser(self):
        """首次点图标显示的面板选择页。记住选择后直接跳到面板。"""
        page = os.path.join(PANELS_DIR, "chooser.html")
        if os.path.isfile(page):
            return self.send_file(page, "text/html; charset=utf-8")
        # 兜底：没有选择页就去状态页
        return self.serve_static("/index.html")

    def serve_switcher(self):
        """提供面板内的「切换面板」脚本（供两个面板共同引用）。

        必须显式禁用缓存：
        该脚本是面板 HTML 之外**唯一**可独立更新的入口。若被浏览器
        启发式缓存住，修好的切换逻辑同样送不到老用户手上 —— 那正是
        面板 HTML + sw.js 预缓存版本号踩过的坑（见 SWITCH_SCRIPT 注释）。
        文件很小（几 KB），每次都取最新版的代价可以忽略。
        """
        target = os.path.join(UI_ROOT, "panel-switch.js")
        if not os.path.isfile(target):
            return self.send_text(404, "Not Found")
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            return self.send_text(500, "Internal Error")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_panel(self, name: str, sub: str):
        base = os.path.realpath(os.path.join(PANELS_DIR, name))
        if not os.path.isdir(base):
            return self.send_text(404, f"panel not installed: {name}")
        rel = sub.lstrip("/") or "index.html"
        target = os.path.realpath(os.path.join(base, rel))
        # 面板目录内的路径穿越防护
        if not (target == base or target.startswith(base + os.sep)):
            return self.send_text(400, "Bad Request")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            # SPA 回退：未知路径交回 index.html 由前端路由处理
            target = os.path.join(base, "index.html")
            if not os.path.isfile(target):
                return self.send_text(404, "Not Found")
        ext = os.path.splitext(target)[1].lower()
        return self.send_file(target, MIME.get(ext, "application/octet-stream"))

    # ── Clash API 反代 ─────────────────────────────────────
    def is_websocket_upgrade(self) -> bool:
        return "websocket" in (self.headers.get("Upgrade") or "").lower()

    def proxy_websocket(self, upstream: str, query: str = ""):
        """把 WebSocket 连接透传到 Clash API。

        面板的**实时流量 / 内存 / 连接 / 日志**图表依赖这几个 WS 端点
        （/traffic、/memory、/connections、/logs），实测 mihomo 对这些
        返回 101。若不做 WS 透传，面板会「连上但图表不动」。

        做法：向本机 Clash API 建一条 WS 握手连接，成功后把客户端的
        101 响应原样回给浏览器，然后双向拷贝字节流。
        """
        secret = read_secret()
        sock = None
        try:
            sock = socket.create_connection(("127.0.0.1", API_PORT), timeout=5)
            # 构造转发到 Clash API 的握手请求，注入密钥
            lines = [f"GET {upstream}{('?' + query) if query else ''} HTTP/1.1"]
            lines.append(f"Host: 127.0.0.1:{API_PORT}")
            for h in ("Upgrade", "Connection", "Sec-WebSocket-Version",
                      "Sec-WebSocket-Key", "Sec-WebSocket-Protocol", "Origin"):
                v = self.headers.get(h)
                if v:
                    lines.append(f"{h}: {v}")
            if secret:
                lines.append(f"Authorization: Bearer {secret}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

            # 读上游握手响应
            sock.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            if b" 101 " not in head.split(b"\r\n")[0]:
                # 上游拒绝升级（例如鉴权失败）：原样返回错误
                self.connection.sendall(head + b"\r\n\r\n" + rest)
                return
            # 回给浏览器：重构握手响应（不暴露上游头里的内网信息）
            resp = ["HTTP/1.1 101 Switching Protocols"]
            for line in head.decode("latin-1").split("\r\n")[1:]:
                if ":" in line:
                    k = line.split(":", 1)[0].strip().lower()
                    if k in ("upgrade", "connection", "sec-websocket-accept",
                             "sec-websocket-protocol", "sec-websocket-extensions"):
                        resp.append(line)
            if not any(l.lower().startswith("upgrade:") for l in resp):
                resp.append("Upgrade: websocket")
            if not any(l.lower().startswith("connection:") for l in resp):
                resp.append("Connection: Upgrade")
            self.connection.sendall(("\r\n".join(resp) + "\r\n\r\n").encode())
            if rest:
                self.connection.sendall(rest)

            # 双向透传
            self.connection.settimeout(None)
            sock.settimeout(None)
            client = self.connection

            def pump(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    for s in (src, dst):
                        try:
                            s.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            t = threading.Thread(target=pump, args=(client, sock), daemon=True)
            t.start()
            pump(sock, client)
            t.join(timeout=5)
        except Exception:
            try:
                self.send_json(502, {"error": "Clash API WebSocket 不可用"})
            except OSError:
                pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def proxy_api(self, upstream: str, query: str = ""):
        """把请求转发到本机 Clash API，并注入服务端持有的密钥。

        仅用于**已经过网关登录态校验**的流量。
        """
        url = f"{API_BASE}{upstream}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, method=self.command)
        secret = read_secret()
        if secret:
            req.add_header("Authorization", f"Bearer {secret}")
        # 透传常见请求头，便于面板正常协商
        for h in ("Content-Type", "Accept"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        # 透传请求体（切节点、改配置等写操作需要）
        body = None
        length = self.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > 0:
            body = self.rfile.read(int(length))
            req.data = body
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "application/json")
                self.send_response(resp.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as exc:
            data = exc.read() or b"{}"
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            return self.send_json(502, {"error": "Clash API 不可用"})

    def do_PUT(self):
        return self.do_GET()

    def do_POST(self):
        return self.do_GET()

    def do_DELETE(self):
        return self.do_GET()

    def send_file(self, path: str, ctype: str):
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self.send_text(500, "Internal Error")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_status(self):
        version_code, version = api_get("/version")
        online = version_code == 200
        payload = {
            "online": online,
            "api_port": API_PORT,
            "version": (version or {}).get("version") if online else None,
            # 供前端生成正确的面板/代理地址（网关下 location.hostname 不可靠）
            "lan_ip": LAN_IP,
            # 当前请求是否经由统一网关（便于前端区分展示）
            "via_gateway": self.headers.get("X-Trim-Userid") is not None,
        }
        if online:
            cfg_code, cfg = api_get("/configs")
            if cfg_code == 200 and isinstance(cfg, dict):
                payload["mode"] = cfg.get("mode")
                payload["mixed_port"] = cfg.get("mixed-port")
                payload["allow_lan"] = cfg.get("allow-lan")
            _, proxies = api_get("/proxies")
            if isinstance(proxies, dict):
                payload["proxy_count"] = len(proxies.get("proxies") or {})
            _, rules = api_get("/rules")
            if isinstance(rules, dict):
                payload["rule_count"] = len(rules.get("rules") or [])
        return self.send_json(200, payload)

    def serve_static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        # 目录穿越防护：解析后必须仍在 UI_ROOT 内
        base = os.path.realpath(UI_ROOT)
        target = os.path.realpath(os.path.join(base, rel))
        if not (target == base or target.startswith(base + os.sep)):
            return self.send_text(400, "Bad Request")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            return self.send_text(404, "Not Found")
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            return self.send_text(500, "Internal Error")
        ext = os.path.splitext(target)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UnixHTTPServer(ThreadingHTTPServer):
    """在 Unix Socket 上监听，供 fnOS 统一网关转发（gatewaySocket）。

    网关会先校验 NAS 登录态，再把请求转发到 ${TRIM_APPDEST}/app.sock。
    网关还会注入可信身份 Header：X-Trim-Userid / X-Trim-Isadmin / X-Trim-Username。
    """

    address_family = socket.AF_UNIX

    def server_bind(self):
        # Unix Socket 无 TCP 概念，跳过 getfqdn 相关的绑定逻辑
        self.socket.bind(self.server_address)


def main():
    """监听器：

      1. 统一网关（Unix Socket）—— **默认启用**，桌面入口 /app/mihomo 走这里，
         复用 NAS 登录态与域名，无需额外开放 TCP 端口
      2. TCP 端口服务（可选）—— 仅当确实需要「不经 NAS 登录态、从局域网
         直接访问状态页」时才开（设 MIHOMO_UI_TCP=1）

    设计取舍：状态页是管理界面，受众是已登录的 NAS 用户，
    因此默认**只开网关通道**，不额外暴露 TCP 端口，减少攻击面。
    """
    servers = []

    # ── 通道 1：统一网关 Unix Socket（默认）──────────────
    sock_path = os.environ.get("MIHOMO_SOCKET", "")
    if sock_path:
        # 启动前移除陈旧 Socket，否则 bind 会失败
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        unix = UnixHTTPServer(sock_path, Handler)
        # Socket 权限：仅属主可访问，避免同机其它用户直连绕过网关
        os.chmod(sock_path, 0o600)
        servers.append((f"unix {sock_path}", unix))

    # ── 通道 2：TCP 端口服务（可选，默认关闭）────────────
    # 仅当需要局域网直连（不经 NAS 登录态）时才开。
    if os.environ.get("MIHOMO_UI_TCP", "0") == "1":
        bind_host = os.environ.get("MIHOMO_UI_BIND", "0.0.0.0")
        tcp = ThreadingHTTPServer((bind_host, UI_PORT), Handler)
        servers.append((f"tcp {bind_host}:{UI_PORT}", tcp))

    if not servers:
        print("no listener configured (set MIHOMO_SOCKET or MIHOMO_UI_TCP=1)", flush=True)
        return

    for label, _ in servers:
        print(f"status page listening on {label}", flush=True)

    # 每个监听器一个线程；主线程等待
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for _, s in servers]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for _, s in servers:
            s.shutdown()
            s.server_close()
        if sock_path and os.path.exists(sock_path):
            os.unlink(sock_path)


if __name__ == "__main__":
    main()
