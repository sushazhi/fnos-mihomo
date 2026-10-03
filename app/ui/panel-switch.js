/* Mihomo fnOS · 面板内「切换面板」浮动入口
 * ---------------------------------------------------------------------------
 * 为什么需要这个脚本（而不是改 URL 加 ?choose=1）：
 *   两个内置面板都是 PWA，各自注册了 Workbox Service Worker，并且都只注册了
 *   一条 NavigationRoute（绑定到预缓存的 index.html）：
 *       Zashboard  : registerRoute(new NavigationRoute(createHandlerBoundToURL("index.html")))
 *       MetaCubeXD : registerRoute(new NavigationRoute(createHandlerBoundToURL("./")))
 *   于是**面板内的任何导航都由 SW 从缓存应答**，根本到不了服务端。
 *   所以「在面板地址后面加 ?choose=1」永远不会生效 —— 那个参数只有
 *   选择页 chooser.html 会读，而它只在访问应用根时才被服务端返回。
 *
 *   结论：切换入口必须活在**面板页面自身的 DOM** 里。本脚本就是它。
 *
 * 为什么单独一个文件（构建期只在 index.html 里插一行 loader）：
 *   * NavigationRoute 只匹配导航请求（request.mode === "navigate"），
 *     普通 <script src> 子资源请求不受它拦截 → 本文件永远从服务端取最新版，
 *     改逻辑不需要重新动面板 HTML，也不需要再动 SW 的预缓存版本号。
 *   * 逻辑只此一份，两个面板共用，不会各写一遍走样。
 *
 * 隔离性：挂在 Shadow DOM 里，面板自身的 Tailwind/base 样式进不来，
 *         也不会污染面板样式；z-index 取最大值压过面板浮层。
 * ---------------------------------------------------------------------------
 */
(function () {
  "use strict";

  // 幂等：面板内的 SPA 路由切换或脚本重复注入都不应产生第二个按钮
  if (window.__MIHOMO_PANEL_SWITCH__) {
    return;
  }
  window.__MIHOMO_PANEL_SWITCH__ = true;

  // 与 chooser.html 共用同一个 key，保证「直接跳另一个面板」后
  // 下次从 fnOS 桌面图标进入时仍然记得这次的选择。
  var STORAGE_KEY = "mihomo/panel";

  // 当前面板名：/panel/zashboard/xxx -> "zashboard"
  var CURRENT = (function () {
    var m = location.pathname.match(/\/panel\/([^/]+)/);
    return m ? m[1] : "";
  })();

  // 应用根前缀：从**本脚本自身的 URL** 反推，而不是猜 location.pathname。
  //
  // 本脚本总是由服务端提供在 <应用前缀>/panel-switch.js（见 ui_server.py 的
  // SWITCH_SCRIPT），所以「脚本 URL 去掉文件名」就是应用前缀，天然兼容：
  //   https://<nas>/app/mihomo/panel-switch.js -> /app/mihomo  （统一网关）
  //   http://<nas>:9092/panel-switch.js        -> ""           （端口直连）
  // 这样就不必写死 appname/网关前缀，面板改名或换前缀都不会失效。
  //
  // 状态页入口的脚本会被提供在 /app/mihomo/status/panel-switch.js，
  // 但状态入口与主入口**共用同一个 Socket**，服务端剥掉各自前缀后内部路径相同。
  // 因此要再剥掉尾部 /status 归一到主入口 —— 否则从状态页切面板会走成
  // /app/mihomo/status/panel/...，等于给同一个面板造出第二套路径与 SW scope。
  var PREFIX = (function () {
    // 优先用 document.currentScript：同步（含 defer）脚本执行期间它就是本脚本，
    // 最准确；只在极老浏览器上才回退到遍历 <script>。
    var self = document.currentScript || null;
    if (!self) {
      var scripts = document.getElementsByTagName("script");
      for (var i = 0; i < scripts.length; i++) {
        if ((scripts[i].src || "").indexOf("panel-switch.js") >= 0) {
          self = scripts[i];
          break;
        }
      }
    }
    if (!self || !self.src) {
      return "";
    }
    var path = "";
    try {
      // self.src 已是解析后的绝对 URL，取 pathname 再砍掉文件名
      path = new URL(self.src, location.href).pathname;
    } catch (e) {
      path = "";
    }
    var cut = path.indexOf("panel-switch.js");
    path = (cut >= 0 ? path.slice(0, cut) : path).replace(/\/+$/, "");
    if (/\/status$/.test(path)) {
      path = path.slice(0, -"/status".length);
    }
    return path;
  })();

  function appUrl(suffix) {
    return PREFIX + suffix;
  }

  var ALL_PANELS = [
    { id: "metacubexd", label: "MetaCubeXD", url: appUrl("/panel/metacubexd/") },
    { id: "zashboard", label: "Zashboard", url: appUrl("/panel/zashboard/") }
  ];

  // 「切换面板」= 回应用根的选择页，并显式带 ?choose=1 强制显示
  //（否则 chooser 会因为已记住选择而立刻跳回当前面板，等于没切换）。
  var ITEMS = [
    { label: "切换面板…", url: appUrl("/?choose=1"), hint: "显示面板选择页" }
  ];
  // 另一个面板可以直接一步跳过去，少一次点击
  ALL_PANELS.forEach(function (p) {
    if (p.id !== CURRENT) {
      ITEMS.push({
        label: p.label,
        url: p.url,
        hint: "直接进入",
        remember: p.id
      });
    }
  });
  // 已经在状态页上时就不再重复给"状态页"入口（CURRENT 为空即状态页）
  if (CURRENT) {
    ITEMS.push({
      label: "状态页",
      url: appUrl("/index.html"),
      hint: "内核运行状态"
    });
  }

  function remember(id) {
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch (e) {
      /* 隐私模式等场景下 localStorage 不可用，忽略即可 */
    }
  }

  var CSS = [
    ":host{all:initial}",
    // 定位：右下角、抬高到面板底栏之上。
    // 两个面板在窄屏都有贴底的导航条（Zashboard 的 .dock 高 4rem 且叠加
    // env(safe-area-inset-bottom)，MetaCubeXD 同样有底部导航），
    // 直接贴 bottom:0 会被盖住或挡到人家按钮；桌面端没有底栏时
    // 只是安静地浮在角落，同样可用。
    ".wrap{position:fixed;right:12px;bottom:calc(5rem + env(safe-area-inset-bottom,0px));",
    "z-index:2147483647;display:flex;flex-direction:column;align-items:flex-end;gap:8px;",
    "font-family:system-ui,-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;",
    "-webkit-tap-highlight-color:transparent}",
    ".fab{display:flex;align-items:center;justify-content:center;gap:6px;height:38px;padding:0 13px;",
    "border:1px solid rgba(127,127,127,.35);border-radius:19px;cursor:pointer;",
    "background:rgba(255,255,255,.92);color:#1f2328;font-size:13px;font-weight:600;",
    "box-shadow:0 2px 10px rgba(0,0,0,.18);backdrop-filter:blur(6px);opacity:.82;",
    "transition:opacity .15s ease-out}",
    ".fab:hover,.fab:focus-visible{opacity:1}",
    ".fab .ico{font-size:15px;line-height:1}",
    ".menu{display:none;flex-direction:column;min-width:170px;padding:5px;",
    "border:1px solid rgba(127,127,127,.32);border-radius:12px;background:#fff;color:#1f2328;",
    "box-shadow:0 6px 24px rgba(0,0,0,.22)}",
    ".wrap.open .menu{display:flex}",
    ".item{display:flex;flex-direction:column;gap:1px;padding:8px 10px;border-radius:8px;",
    "text-decoration:none;color:inherit;cursor:pointer}",
    ".item:hover,.item:focus-visible{background:rgba(127,127,127,.14)}",
    ".item .t{font-size:13px;font-weight:600}",
    ".item .h{font-size:11.5px;opacity:.62}",
    "@media (prefers-color-scheme:dark){",
    ".fab{background:rgba(22,27,34,.92);color:#e6edf3}",
    ".menu{background:#161b22;color:#e6edf3}",
    "}"
  ].join("");

  function build() {
    var host = document.createElement("div");
    // 不用 <body>：某些面板会把 body 整体替换/重挂载，挂 documentElement 更稳
    var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : null;
    if (!root) {
      return; // 不支持 Shadow DOM 就不注入，避免污染面板样式
    }

    var style = document.createElement("style");
    style.textContent = CSS;
    root.appendChild(style);

    var wrap = document.createElement("div");
    wrap.className = "wrap";

    var menu = document.createElement("div");
    menu.className = "menu";
    menu.setAttribute("role", "menu");

    ITEMS.forEach(function (item) {
      var a = document.createElement("a");
      a.className = "item";
      a.href = item.url;
      a.setAttribute("role", "menuitem");

      var t = document.createElement("span");
      t.className = "t";
      t.textContent = item.label;
      a.appendChild(t);

      if (item.hint) {
        var h = document.createElement("span");
        h.className = "h";
        h.textContent = item.hint;
        a.appendChild(h);
      }

      // 直接进入另一个面板时，同步记住选择，保持与选择页行为一致
      if (item.remember) {
        a.addEventListener("click", function () {
          remember(item.remember);
        });
      }
      menu.appendChild(a);
    });

    var fab = document.createElement("button");
    fab.className = "fab";
    fab.type = "button";
    fab.title = "切换面板 / 查看状态";
    fab.setAttribute("aria-label", "切换面板");
    fab.setAttribute("aria-expanded", "false");

    var ico = document.createElement("span");
    ico.className = "ico";
    ico.textContent = "⇄";
    fab.appendChild(ico);

    var label = document.createElement("span");
    label.textContent = "面板";
    fab.appendChild(label);

    function setOpen(open) {
      wrap.classList.toggle("open", open);
      fab.setAttribute("aria-expanded", open ? "true" : "false");
    }

    fab.addEventListener("click", function (ev) {
      ev.stopPropagation();
      setOpen(!wrap.classList.contains("open"));
    });

    // 点菜单外部或按 Esc 收起
    document.addEventListener("click", function () {
      if (wrap.classList.contains("open")) {
        setOpen(false);
      }
    });
    menu.addEventListener("click", function (ev) {
      ev.stopPropagation();
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") {
        setOpen(false);
      }
    });

    wrap.appendChild(menu);
    wrap.appendChild(fab);
    root.appendChild(wrap);

    (document.documentElement || document.body).appendChild(host);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
