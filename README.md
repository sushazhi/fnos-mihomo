# mihomo — 飞牛 fnOS 原生应用参考实现

这是一个**可直接构建**的飞牛 fnOS（`.fpk`）原生应用参考实现：把
[mihomo](https://github.com/MetaCubeX/mihomo)（Clash.Meta）内核做成 fnOS 应用，
随系统自启，提供混合代理端口 + Clash API + 内置状态页与两个管理面板。

> 配套开发文档：`docs/mihomo-fnos-开发文档.md`（不随本仓库分发）

## 功能

- **开机自启**：由 fnOS 应用中心托管 `cmd/main` 的 start/stop/status
- **混合代理端口**（默认 7890）：HTTP / SOCKS5
- **Clash API**（默认 9090）：供 MetaCubeXD 等面板连接
- **内置状态页 + 两个管理面板**：桌面入口走**统一网关** `/app/mihomo`（复用 NAS 登录态）；
  首次点图标可选 MetaCubeXD 或 Zashboard；面板经同源反代访问 Clash API，**密钥不下发前端**
- **订阅支持**：在「应用设置」填订阅链接，启动时拉取并强制修正
  `mixed-port` / `allow-lan` / `external-controller`（避免订阅覆盖导致局域网不可用）
- **最小权限**：默认 `run-as=package`，不使用 root

## 快速开始

```bash
# 1. 构建（自动下载 fnpack / mihomo 内核 / geo 数据 / 两个面板）
python build.py --arch x86                      # → mihomo-1.19.32-amd64.fpk (+ .sha256)
python build.py --arch arm                      # → mihomo-1.19.32-arm64.fpk

# 2. 安装到 fnOS 测试机
appcenter-cli install-fpk mihomo-1.19.32-amd64.fpk
appcenter-cli start mihomo
```

> 构建脚本是 `build.py`（**纯标准库、跨平台**，Windows / Linux / macOS 都能跑）。
> `--force` 强制重新下载所有依赖。
>
> **Windows 上也能构建**：会自动下载官方 `fnpack-1.2.3-windows-amd64.exe`
> 并按开发机平台缓存到 `.local-build/`。注意 Windows 版 fnpack 会把包内文件
> 统一写成 `0666`（丢掉执行位），`build.py` 在打包后会**重写 tar 里的 mode**、
> 把 `cmd/*` 修正为 `0755` —— 否则装到设备上生命周期脚本不可执行。

### 版本号：默认跟随 mihomo 内核

不传 `--version` 时，**包版本 = 内核版本**：构建时取 mihomo 的
`/releases/latest` tag，去掉 `v` 前缀后写进 manifest 的 `version`：

```
内核 tag v1.19.32  →  manifest version = 1.19.32  →  mihomo-1.19.32-amd64.fpk
```

- `python build.py --version 1.0.7` 可显式覆盖，以你给的为准。
- 内核版本取不到（断网构建）或 tag 不是数字版本（如 `alpha-20240920`）时，
  自动回退读 `manifest` 里手写的 `version`（并在日志里 WARN 提示）。
- 因此 `manifest` 的 `version` 此时是**兜底值**，正常联网构建会被内核版本覆盖。

> ⚠️ 飞牛 manifest 的 `version` 官方示例一律是裸数字点分（`1.0.0`、`2.1.3-beta`），
> 所以这里会**去掉 tag 的 `v` 前缀**（`v1.19.32` → `1.19.32`），不直接写 `v1.19.32`。

### 下载源与超时

构建期下载遵循同一条策略：**先直连，失败再依次走加速源** —— 直连能通就不绕道第三方。
加速源用**前缀拼接**原始 URL，依次为 `https://gh.dpik.top/`、`https://gh-proxy.org/`：

```
https://gh.dpik.top/https://github.com/MetaCubeX/mihomo/releases/download/<tag>/<asset>
```

适用范围是**GitHub 来源**：mihomo 内核、geo 数据、两个面板资产。

> `fnpack` 是**例外，走纯直连**：它从 `static2.fnnas.com`（飞牛自家静态站）下载，
> 不是 GitHub 资产，套第三方前缀代理没有收益，只会多一个失败点和一轮超时等待。

每个源都有**单源超时**（默认 60s），失败立即换下一个源，不会在某个被墙地址上
挂满十几分钟。可用环境变量调整：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `MIHOMO_DL_TIMEOUT` | `60` | 每个下载源的超时秒数 |
| `MIHOMO_FNPACK_TIMEOUT` | `120` | fnpack 自身下载的超时秒数 |
| `MIHOMO_API_TIMEOUT` | `15` | GitHub API（取版本号）的超时秒数 |
| `MIHOMO_DL_MIRRORS` | 内置两个源 | 加速源列表，逗号分隔；**留空则只用直连** |

例如在完全能直连的网络里关掉加速源：

```bash
MIHOMO_DL_MIRRORS="" python build.py --arch x86
```

带向导的自动化安装：

```bash
cat > config.env <<'EOF'
wizard_mixed_port=7890
wizard_api_port=9090
wizard_api_secret=your-strong-secret
wizard_subscription_url=https://example.com/sub
wizard_allow_lan=true
EOF
appcenter-cli install-fpk mihomo-1.19.32-amd64.fpk --env config.env
```

> `config.env` 含密钥，**不要提交到版本库**。

## 自动更新（GitHub Actions）

仓库内置两个 workflow：

| 文件 | 触发 | 作用 |
| --- | --- | --- |
| `.github/workflows/auto-update.yml` | **每周五 10:00（北京时间）** | 检查上游是否有新版本；有则改 `manifest`、提交、构建、发 Release；没有则整段跳过 |
| `.github/workflows/build-fpk.yml` | 推 `v*` tag / 手动 / 被上面复用 | 双架构构建 + 自检 + 发 Release |

检查范围是三个上游：

- `MetaCubeX/mihomo`（内核）
- `MetaCubeX/metacubexd`（MetaCubeXD 面板）
- `Zephyruso/zashboard`（Zashboard 面板）

**版本号规则**（内核更新与面板更新分开处理）：

| 情况 | 包版本示例 | 说明 |
| --- | --- | --- |
| 内核更新 | `1.19.32` | 版本号 = 内核版本（去掉 `v` 前缀），面板序号归零 |
| 仅面板更新 | `1.19.32.1` → `1.19.32.2` → … | 内核没变时，为面板更新单独出包，第四段序号逐次递增 |

第四段用**纯数字**（而不是 `-r2` 这类字母后缀）：官方文档只说 `version` 形如
`1.0.0` / `2.1.3-beta`，**没有说明版本如何比较大小**。若飞牛按 semver 比较，
`-r2` 属于 pre-release，会**小于** `1.19.32`，设备会当成降级而不发更新；
`1.19.32.1` 在字符串比较和数字段比较两种规则下都更大，排序无歧义。

判定与去重规则：

1. 探测三个上游的 latest release，任一探测失败即**报错退出**——
   「查不到」绝不当成「没更新」（否则会长期不发版却一直绿灯）。
2. 读 `scripts/pinned-versions.json`（记录上次打包用的上游版本），
   判断内核是否变化、面板是否变化。
3. 按上表算出本次包版本，再用 **Release tag 是否存在**去重（tag = `v` + 包版本）。
4. 有更新时把包版本写进 `manifest` 的 `version`，并把本次面板版本写回
   `pinned-versions.json`，一并提交后复用 `build-fpk.yml` 构建发布。

需要 `contents: write` 权限；若默认分支开了分支保护，自动提交会被拒，
届时需给 `github-actions[bot]` 放行或改成 PR 流程。

也可以只在本地看一眼当前落后多少（不影响仓库）：

```bash
python scripts/check-updates.py
```

> `scripts/pinned-versions.json` 是**状态文件**，由 workflow 自动维护。
> ⚠️ 不要手动删里面的键：删掉会被当成「首次运行」，从而错过一次面板更新。

## 目录结构

```
.
├── manifest                 # 应用元数据（platform=x86/arm）
├── build.py                 # ★ 统一构建脚本（跨平台，含面板切换入口注入 + 打包后修权限）
├── config/
│   ├── privilege            # run-as: package
│   └── resource             # 共享目录声明
├── cmd/                     # 生命周期脚本（9 个）
├── wizard/                  # install / config / uninstall 表单
├── app/                     # ← 会被打进 app.tgz 的全部内容
│   ├── ui/                  # 桌面入口配置 + 状态页前端 + 图标
│   │   ├── panel-switch.js  #   面板内「⇄ 面板」切换按钮（服务端以 no-cache 提供）
│   │   ├── index.html       #   状态页
│   │   ├── images/          #   桌面入口图标（icon_64.png / icon_256.png）
│   │   └── config           #   桌面入口声明（两个网关入口）
│   ├── panels/              # 两个面板 + 选择页（构建时下载）
│   ├── ui_server.py         # 状态页/面板服务（必须在 app/ 内才会被打包）
│   └── mihomo               # 构建时下载（见 .gitignore）
├── .local-build/            # 构建缓存（fnpack / 内核 / geo / 面板，不入库）
└── scripts/                 # 工具脚本
    ├── panel-switch-boot.js #   面板内联引导脚本模板（构建与测试共用的唯一来源）
    ├── check-updates.py     #   检查三个上游是否有新版本（workflow 与本地都用）
    └── pinned-versions.json #   自动更新状态文件（勿手删键，见上文）
```

> **图标是静态文件，不再由脚本生成。** 四份图标均已入库，替换图标请直接覆盖：
> `ICON.PNG` / `ICON_256.PNG`（包图标，64 / 256）与
> `app/ui/images/icon_64.png` / `icon_256.png`（桌面入口，64 / 256）。
> 规格：正方形 RGBA PNG、单文件 ≤1024KB、主体为圆角矩形（不要直角满铺）、64px 下仍清晰。

## 环境要求

- **打包机**：任意装有 **Python 3.8+** 的机器（Windows / Linux / macOS）。
  仅用标准库，**无需** `curl`/`unzip`/`tar`/`zstd`；`fnpack` 会自动下载并缓存。
- **目标机**：飞牛 fnOS 1.1.3100+（`os_min_version`）

## 验证状态

已在模拟安装环境（伪造 `TRIM_*` + 假内核）验证：

| 项目 | 结果 |
| --- | --- |
| `main start` / `stop` / `status`(运行) | 0 / 0 / 0 |
| `main status`(未运行) | **3**（符合官方契约） |
| 未知命令 | 1，并写 `TRIM_TEMP_LOGFILE` |
| start / stop 幂等性 | 通过 |
| 端口冲突（被他人占用） | 报错退出 1，**不 kill 他人进程** |
| 配密钥后健康检查 | 通过（带 Authorization） |
| **状态页端点白名单** | `/api/connections`、`/api/proxies`、`/api/configs`、`/api/rules` **全部 403**；`/api/status` 正常且不回显 secret |
| `config_callback` 留空保持 | 通过（不误清空密钥） |
| `settings.env` 权限 | 0600 |
| 状态页路径穿越防护 | 已拦截（6 种编码变体零泄露） |
| 图标规格（正方形/圆角/尺寸/体积） | 通过 |

## 访问模型：统一网关 + 端口双通道

管理界面**只走统一网关**，不额外开 TCP 端口：

| 组件 | 访问模型 | 需要登录态？ |
| --- | --- | --- |
| **内置面板**（MetaCubeXD / Zashboard） | **统一网关** `/app/mihomo` → `${TRIM_APPDEST}/app.sock` | 是（网关强制校验） |
| 代理端口 `7890` | 独立 TCP 端口 | **否** —— 局域网设备/容器没有 cookie |
| Clash API `9090` | 独立 TCP 端口 | **否** —— 第三方客户端无 cookie |

> 后两者**必须**是独立端口（服务非浏览器客户端）；
> 管理面板正相反，**就该**要求登录态 —— 三种消费者性质不同，不要一刀切。

### 内置两个面板，首次点图标选择

| 面板 | 说明 | 首次是否要填后端 |
| --- | --- | --- |
| **MetaCubeXD**（推荐） | mihomo **官方面板**，与内核同组织，兼容性最有保障 | **否**，构建期注入同源模式 |
| **Zashboard** | UI 更现代，移动端体验好 | 是，需在面板内加一次后端地址 |

选择记在 `localStorage`（按浏览器隔离，多用户各自记住）。

**怎么换面板**（改地址无效，见下）：

- 面板**右下角的「⇄ 面板」浮动按钮** —— 在 fnOS 桌面 iframe 里也能用，
  这是唯一不需要动地址栏的方式，点开即可切到另一个面板；
- 或者直接访问 `https://<NAS>/app/mihomo/?choose=1` 回到选择页；
- 想直接进某个面板：`.../app/mihomo/panel/metacubexd/`、`.../app/mihomo/panel/zashboard/`。

> ⚠️ 在面板页面地址后面手动加 `?choose=1` **不会有任何效果**。
> 两个面板都是 PWA，各自注册了 Workbox 的 `NavigationRoute`，面板内的
> 导航请求会**被 Service Worker 用预缓存的 `index.html` 直接应答**，
> 请求根本到不了服务端，服务端也就没机会返回选择页。切换入口必须活在
> 面板页面自身的 DOM 里 —— 这就是那个浮动按钮存在的原因。

### 面板如何免填密钥

面板需要完整 Clash API（含写操作）。本应用按**通道分级授权**：

| 通道 | 授权范围 |
| --- | --- |
| 经**统一网关**（已登录） | 放行**完整** Clash API，密钥由服务端注入 |
| **局域网直连** | 仅放行**只读白名单**（如 `/version`） |

判定依据是网关注入的 `X-Trim-Userid`。实测：网关下切节点（`PUT /proxies/X/select`）可用；
局域网直连访问敏感端点 **403**、写操作 **404**。

Socket 权限 `0600`，停止时自动清理。**若确实需要局域网直连**状态页，
可设 `MIHOMO_UI_TCP=1` 额外监听 TCP `:9092`（默认关闭）。

> ⚠️ **接网关的两个必做改造**（实测踩坑，端口模式下不会暴露）：
> 1. **网关转发保留完整前缀** —— 必须在入口剥离 `gatewayPrefix`，否则全部 404
> 2. **AF_UNIX 下不能用 `address_string()`** —— 会抛 `IndexError` 导致请求崩溃
>
> 详见开发文档 7.3。

## 安全说明（重要）

状态页默认绑定 `0.0.0.0`（`:9092`），供 fnOS 桌面入口与局域网访问。
为此**只开放极小只读白名单**，状态页所需数据由服务端聚合后输出：

```python
ALLOWED_PROXY_ENDPOINTS = {"/version"}
```

**刻意不开放**（避免泄露）：`/connections`（访问记录）、`/proxies`（节点名）、
`/rules`（规则集）、`/configs`（**含 secret**）。

如需更严格，可限制为本机访问：

```bash
MIHOMO_UI_BIND=127.0.0.1   # 默认 0.0.0.0
```

> ⚠️ 早期版本曾用「`/api/*` 全量前缀透传」，导致**无凭据的局域网客户端
> 可读取全部代理访问记录、甚至回显 API 密钥**。已修复为白名单 + 默认拒绝。
> 详见开发文档 10.5 的反面教材分析。

> ⚠️ 以上**不等于真机测试**。上架前必须在真实 fnOS 设备上按
> 开发文档 11.2 的验收清单完整验证。

## 已知取舍

- **TUN 模式未启用**：需要 `/dev/net/tun` 与 `CAP_NET_ADMIN`，而 `config/privilege`
  没有 capability 字段。已知的落地做法是 `run-as=root`（fnProxy 即如此），风险较高；
  曾寄望的"保持 package 用户 + `setcap`"路线**已在真机（设备 `OECT`）被端到端否决**：
  四个前置条件（`/dev/net/tun`=0666、`/vol1` 无 `nosuid`、`security.capability` 可写可读、
  `NoNewPrivs=0` 且 `CapBnd` 含 net_admin）**全部满足**，但已打 capability 的副本与未打的副本
  得到**完全相同的 EPERM** —— file capability 未生效。**故 TUN 若要做只能 `run-as=root`**，
  代价是把面板 / 订阅解析 / Clash API 全部拉进 root。本应用因此仍默认走代理端口模式。
  详见开发文档 5.1、10.6；探查脚本见 `research/tun-probe/`，
  实测数据见 `research/verify-network.md` 文末（两者均不随本仓库分发）。
- **默认架构相关**：为避免装错架构，`platform` 分 x86 / arm 分别打包，
  不使用 `all`。
- **架构命名有两层，别弄混**：`manifest` 的 `platform` 字段**只能**是
  `x86` / `arm` / `all`（飞牛官方 Manifest 文档的枚举值，填 `amd64`/`arm64`
  是非法值，会导致应用中心识别不了架构、装错包）。而**产物文件名**用
  `amd64` / `arm64`，与飞牛官方发布命名（`fnpack-1.2.3-linux-amd64` /
  `-linux-arm64`）一致 —— `arm` 太含糊（armv7 还是 arm64 分不清）。
  CLI 的 `--arch` 参数取值仍是 `x86` / `arm`。
  **不要**把 `manifest` 的 `platform` 改成 `amd64`/`arm64`。

## 许可

mihomo 内核采用 GPL-3.0；本参考实现脚手架部分可自由使用。
**若你要分发包含 mihomo 内核的 fpk，请自行确认 GPL-3.0 合规**
（随包附带许可证全文，并在安装时展示协议）。
