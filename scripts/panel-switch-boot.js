/* 面板切换入口的「引导脚本」——**构建期模板**，不是运行时资源。
 *
 * 作用：在面板页面里算出应用根前缀，再动态插入
 *       <应用前缀>/panel-switch.js
 * 真正干活的切换 UI 在 app/ui/panel-switch.js（由 ui_server.py 以 no-cache 提供）。
 *
 * 为什么必须动态插入，而不是写死 <script src="../../panel-switch.js">：
 *   面板是 PWA，Workbox 的 NavigationRoute 会把面板内的**任何导航**都用预缓存
 *   的 index.html 应答。于是访问深链接
 *       /app/mihomo/panel/zashboard/proxies
 *   时页面内容是缓存的 index.html，但浏览器解析相对 URL 仍以**地址栏**为基准：
 *       ../../panel-switch.js -> /app/panel-switch.js   ✗ 404
 *   所以相对路径在深链接下必然失效，必须按 location.pathname 反推前缀。
 *
 * 为什么是**独立文件**而不是直接写在构建脚本的 heredoc 里：
 *   该脚本要同时被「注入到面板 HTML」和「测试里真执行」两处使用。
 *   之前测试里另抄了一份，结果引导脚本一改，测试就测的是旧版本（已踩）。
 *   放在这里作为唯一来源，build.py 与测试都读它，改一处即全同步。
 *
 * 约定：构建时会被包进 <script> 标签（见 build.py 的 inject_panel_switch），
 *       因此文件内**不得**出现结束标签的字面量（会提前闭合外层 script）。
 *       构建脚本对此有防御性检查，一旦出现就直接拒绝注入。
 *       可以含注释。
 */
(function () {
  // 非贪婪匹配到**第一个** /panel/<name>，故任意深度的深链接都能推出正确前缀：
  //   /app/mihomo/panel/zashboard/proxies -> "/app/mihomo"
  //   /panel/zashboard/（端口直连）        -> ""
  var m = location.pathname.match(/^(.*?)\/panel\/[^\/]+\/?/);
  var s = document.createElement("script");
  // 补上开头的 "/" 使其**从服务端根解析**。
  // 若写成相对的 "panel-switch.js"，前缀为空（端口直连模式）时会解析成
  // /panel/panel-switch.js（404）—— 端口直连时前缀恰好为空，必踩。
  s.src = (m ? m[1] : "") + "/panel-switch.js";
  document.head.appendChild(s);
})();
