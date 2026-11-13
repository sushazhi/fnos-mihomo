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
- **Cloudflare 优选 IP**：把优选 IP（外部结果地址定时拉取，或由内置测速在本机现测）
  注入内核 `hosts`，使走 DIRECT 出站的 CF 前置站点直连到优选 IP；**IPv4 / IPv6 都支持**
  （见下文专节）
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
│   ├── cfopt.py             # CF 优选：fetch / measure / inject 三个子命令（同上）
│   └── mihomo               # 构建时下载（见 .gitignore）
├── tests/                   # cfopt.py 的黑盒回归测试（不打进包）
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

## Cloudflare 优选 IP：让 DIRECT 出口也用上优选结果

### 要解决的问题

Cloudflare 免费版会把访客调度到「就近但不一定最快」的 anycast IP，运营商绕路时
延迟/丢包会明显变差。`CloudflareSpeedTest` 之类的工具能测出一批真正快的 CF IP；
本功能把这些 IP 注入内核，使**走 DIRECT 出站的 CF 前置站点直连到优选 IP**
（不改写任何代理节点的 server）。

### 怎么用

「应用设置 → Cloudflare 优选 IP」：

| 字段 | 说明 |
| --- | --- |
| 优选来源 | `外部优选结果（URL）`：拉取现成列表；`内置测速（本机现测）`：由本机自己测 |
| 优选结果地址 | 来源选 URL 时必填：`result.csv` 或纯文本 IP 列表的**直链**；留空 = 保持现有，填 `off` = 关闭 |
| 走优选 IP 的域名 | 逗号分隔。`example.com` 覆盖该域名**及其所有子域**；`*.example.com` 只覆盖一级子域 |
| 刷新间隔 | 每 6 / 12 / 24 小时，或「仅应用启动时刷新一次」（`0`） |

选「内置测速」时还有一页参数（留空即用默认值）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| 候选网段 | Cloudflare 官方 v4+v6 网段 | 逗号分隔的 CIDR，从这些网段里抽样 |
| 测速域名（SNI） | `speed.cloudflare.com` | 下载测速时使用的域名 |
| 候选 IP 数量 | `400` | 每次抽样的候选总数 |
| 测速并发数 | `48` | 并发 TCP 连接数 |
| 单 IP 超时 | `1500` 毫秒 | TCP 握手超时，线路慢可调大 |
| 附加下载测速 | 关 | 对最快的若干 IP 再测一次下载速度（更准，也更费带宽） |

**优选不是一次性的。** 保存设置只是给了一个初始值；真正让它「一直有效」的是后台刷新：
每 N 小时重测/重拉一次，拿到新结果后走同一条「注入 → 校验 → 换盘 → 热重载」。
间隔按线路变化频率选择：稳定线路 24 小时够用，抖动大的用 6 或 12 小时。
每次热重载都不断开既有连接，所以刷新本身是很轻的动作。

保存后重启应用生效。也可以立刻手动刷新一次 —— 注意 `cmd/main` 依赖 `TRIM_*` 环境变量，
直接跑会报错退出，所以要带上它们（路径以实际安装目录为准）：

```bash
sudo TRIM_APPDEST="<应用安装目录>" TRIM_PKGVAR="<数据目录>" \
  "<应用安装目录>/cmd/main" cfopt-refresh
```

### 不知道哪些站点套了 CF？用 `cfopt-scan`

域名清单要手填，但「哪些站点其实套了 Cloudflare」可以让工具先扫一遍：

```bash
sudo TRIM_APPDEST="<应用安装目录>" TRIM_PKGVAR="<数据目录>" \
  "<应用安装目录>/cmd/main" cfopt-scan "tjupt.org,www.example.com"
```

结果写到 `<数据目录>/cfopt-scan.txt`，每行 `域名<TAB>IPv4数<TAB>IPv6数<TAB>结论`，
结论为 `套 CF` / `非 CF` / `解析失败`；已在清单里的会额外标注。判定依据是 Cloudflare
官方网段（`api.cloudflare.com/client/v4/ips`，已内置在 `cfopt.py`，不联网拉取）。
不传参数则只扫描当前已注入的域名。

> **为什么必须手填、不能「检测到就自动生效」**：注入靠顶层 `hosts`，而 hosts 只做
> **字面域名匹配**，没有任何通配符能表达「所有域名」（`'*'` 会把全网改成同一组 IP）。
> 而出站层也无法改写目标地址（内核里 `override-address`/`dest-address` 均不存在）。
> 另外 `direct-nameserver` 只服务 **DIRECT 出口**，而套 CF 的站点往往被订阅规则送进
> 代理，此时优选 IP 根本不参与解析 —— 所以本功能只对**走 DIRECT** 的站点有意义。
> `cfopt-scan` 能做的就是把「哪些域名值得填进来」这一步自动化。

运行期生成物（都在 `TRIM_PKGVAR`）：

| 文件 | 用途 |
| --- | --- |
| `cfopt-ips.txt` | 解析/测速后的优选 IP，**每个协议族各取前 4 个** |
| `cfopt-hosts.txt` | 客户端 hosts 片段（含 IPv4 与 IPv6），供不走代理的场景使用 |
| `cfopt-history.log` | 内置测速历史：时间、候选数、可达数、采用数、最快延迟 |
| `config.yaml.cfopt.bak` | 注入前的配置备份 |
| `cfopt-rejected.yaml` | 仅在注入后校验失败时留存，用于排查 |

### 生效原理（逐条对照过源码）

1. 顶层 `hosts:` 由 `config.parseHosts` 写入 `resolver.DefaultHosts`（`*trie.DomainTrie`），
   在 `executor.ApplyConfig → updateHosts` 中赋值，**与 `dns.enable` 是否开启无关**；
2. `resolver.LookupIPWithResolver` / `LookupIPv4/6WithResolver` 在**任何 DNS 之前**
   先查 `DefaultHosts`，命中即返回；
3. DIRECT 拨号时 `metadata.RemoteAddress()` 仍是域名，经
   `dialer.WithResolver(resolver.DirectHostResolver)` 走同一条链路 → 命中优选 IP；
4. 键 `+.example.com` 经 `trie.Insert` 会同时写入「裸域精确命中」与「子域通配」两态；
5. rules 首次匹配即止，因此注入的 `DOMAIN-SUFFIX,…,DIRECT` 被放在 `rules:` 最前面。

注入形态如下；每行都带 `# cfopt:` 标记，重新注入时整行剔除，因此重复执行幂等，
也不会随订阅更新不断堆积：

```yaml
hosts:
  '+.example.com': # cfopt:hosts
    - 104.16.0.1 # cfopt:hosts
    - 104.17.0.2 # cfopt:hosts
rules:
  - 'DOMAIN-SUFFIX,example.com,DIRECT' # cfopt:rule
```

### ⚠️ 前提与边界

- **流量必须经过本应用的混合代理端口**。内核只有在「拿到域名」时才能用 hosts 改写解析结果：
  浏览器/客户端把域名交给代理（HTTP CONNECT / SOCKS5 带域名）→ 生效；
  而应用自己解析 DNS 后按 IP 直连 → 内核根本看不到域名，hosts 无从介入。
  后者请把 `cfopt-hosts.txt` 追加到客户端 `/etc/hosts`，或作为局域网 DNS
  （AdGuard Home / dnsmasq）的 hosts 重写。
- **为什么不走 TUN**：本应用以 `package` 用户运行，无法创建 TUN（见「已知取舍」）。
- **IPv4 与 IPv6 都支持**（这点容易按老经验搞反，展开说清）：本应用生成的配置不含顶层
  `ipv6`，而内核该键的默认值是 `true`，`executor.updateGeneral` 里
  `resolver.DisableIPv6 = !general.IPv6` 因而得到 `false`。更关键的是：DIRECT 默认
  （`prefer: DualStack`）拨号走 `network="tcp"`，落到 `LookupIPWithResolver` ——
  它命中 hosts 时直接 `return node.IPs`，**根本不经过 `DisableIPv6`**，
  随后 `dualStackDialContext` 把 v4/v6 各成一组并发竞速，先连上者胜。
  所以同一个域名下同时写 IPv4 与 IPv6 优选 IP，两会一起参与。
  只有「订阅显式设置顶层 `ipv6: false`」且「用 `ip-version: ipv6-only` 强制单栈」时，
  IPv6 才会被 `ErrIPv6Disabled` 拦掉；反之网络只有 IPv4 时，内核会自然只走 IPv4。
- **这些域名会被强制走 DIRECT**：注入规则的优先级高于订阅原有规则。若订阅本就
  有意把其中某个域名走代理，请不要把它填进来。
- **IP 类规则看到的是优选 IP**：内核在规则匹配前就先用 hosts 把 `DstIP` 填成优选 IP，
  因此 `GEOIP` / `IP-CIDR` 规则匹配到的是优选 IP 而非站点真实 IP。把
  `DOMAIN-SUFFIX` 置顶正是为了避免落到这些规则上。
- **多 IP 是每次随机取一个**：内核用 `randv2.IntN` 随机取，无健康检查、无轮询、无失败记忆。
  所以只保留最快的 4 个（`CFOPT_TOP`，见 `cmd/main`，**按协议族各 4 个**），
  写得越多命中慢 IP 的概率越大。
- **已建立的连接不会切换**：热重载不断开既有连接，keep-alive 期间仍用旧 IP；
  优选 IP 劣化后需要客户端重连（或 `DELETE /connections`）。
- **总开关**：把「优选结果地址」填 `off` 保存并重启，注入会被整块清除，恢复订阅原始配置。

### 注入的安全性

- 注入结果先写到 `config.yaml.cfopt-new`，用 `mihomo -t` 校验。该参数**只做解析**
  （`main.go` → `executor.Parse` → `config.Parse`，**不调用 `ApplyConfig`**、不绑定任何端口），
  因此可与正在运行的内核并存；**通过后才原子替换** `config.yaml`。校验失败则原配置一字不改，
  被拒内容留在 `cfopt-rejected.yaml`。
- 热重载优先走 `PUT /configs?force=true`，失败退回 `SIGHUP`；两者都失败也只会让新优选 IP
  在下次重启时生效，不会把正在运行的代理搞挂。
- 拉取失败时沿用上一次的 IP 列表（只有解析成功才覆盖 `cfopt-ips.txt`），
  不会因一次网络抖动清空配置；内置测速**全部候选都不可达**时同样如此。
- **按域名选 IP：映射即权威，选不出就不注入**。`map` 为每个域名实测候选 IP 写进
  `cfopt-ips.txt.map`；`inject` 只给**有可用映射**的域名写 `hosts` + 置顶 `DIRECT`，
  没有映射的域名**跳过**、保持订阅原有策略（通常走代理）。
  原因：把选不出 IP 的域名硬塞全局列表，实测会让站点 TLS 直接失败
  （2026-11-13：清单 272 条时 hdwing / ourbits / 1ptba 全部打不开，已回滚）。
  没有映射文件时仍按旧行为走全局列表（老部署不受影响）。
- **映射带 TTL + 域名指纹**：`map` 结果在 12 小时内且域名集合未变时直接复用
  （`CFOPT_MAP_TTL` 可调，`CFOPT_MAP_FORCE=1` 强制重测）。启动早期的 `cached` 注入
  **不再重测**，因此重启不会因为「每个域名各测一遍 TLS」把代理可用时间推迟几分钟。
- **注入只合并同名键**：`hosts` 里原有的其它条目（包括与注入域名同基名的兄弟域、
  订阅自己写的记录）一律原样保留，只有当订阅已经写过**同一个键**（如 `+.example.com`）
  时才合并，以免产生 YAML 重复键。注入会跟随已有块的实际缩进（2 空格 / 4 空格都可以），
  **单行**流式写法 `hosts: {a: 1.2.3.4, b: [1.2.3.4, 5.6.7.8]}` 也能正确转换、
  不会把内层数组切坏；`rules` 的**单行**流式写法 `rules: ['DOMAIN-SUFFIX,a.com,PROXY']`
  同样会被转成块式，再把注入的 `DOMAIN-SUFFIX,…,DIRECT` 放到最前面。
  （跨行的流式写法会被判为「不支持」而整次放弃 —— 会记日志、不会写坏配置。）
- **`hosts` 与 `rules` 两边都能写才动手**，否则整次放弃、配置文件一个字节都不改。
  只写 `hosts` 而不置顶 `DIRECT`，这些域名仍会按订阅策略走代理、由代理端远端解析，
  优选映射根本用不上（改了配置却毫无效果）；只插 `DIRECT` 而没有 `hosts` 映射，
  则等于把这些域名从走代理反转成直连，比不注入更糟。遇到无法识别的写法会记一条
  `cfopt: <hosts|rules> 写法不支持，本次不注入任何内容`。
  ⚠️ **配置里完全没有 `rules` 键时也按「不支持」处理**：本应用自己生成、或从订阅写入的配置
  一定带 `rules:`（模板末尾就是 `- MATCH,DIRECT`），所以这只可能是你手写的配置。那种配置下
  若凭空补一条 `MATCH,DIRECT`，会把本该走代理的流量静默改成**全直连** —— 比不注入更糟，
  而我们又无从知道你想要什么兜底策略，因此宁可不注入（日志会额外说明「配置里没有 rules 键」）。
  `hosts` 键缺失则不受影响：新建一个 `hosts:` 只影响我们注入的那几个域名本身。
- 客户端 `cfopt-hosts.txt` 只在**真正注入了优选映射**时才会生成 / 刷新；功能关闭或本次
  注入被放弃时会把它删掉，避免你按下面的说明去用时拿到一份与内核实际状态相反的文件。
  例外：**有域名但连一份 IP 列表都没有**（拉取失败又没有缓存）时，配置与片段都保持不动 ——
  那属于暂时拿不到数据，留着上次的成功结果比删掉更有用；删除也只看本次运行之前的文件，
  不会误删另一个并发刷新刚写出的新片段。
- 所有用户可控的值（优选地址、域名、订阅链接、API 密钥）写入 `settings.env` 时**一律用
  单引号包裹**，避免值里的 `;` `&` `` ` `` `$()` 在 `. settings.env` 时被当成命令执行；
  含单引号的值会被直接拒绝（否则会破坏引号包裹）。
  ⚠️ **改动这层包裹时必须同时改所有读取方**：`cmd/main` 用 `. settings.env`，由 shell 自己
  剥引号；而 `app/ui_server.py` 是**逐行解析**的，必须同样剥掉一层成对引号
  （`unquote_env_value()`）。只改写入方、漏改读取方，会让状态页/面板拿到带引号的密钥 →
  对 Clash API 的请求全部 **401** → 页面显示「后端不可达 / 离线」，而内核其实完全正常
  （2026-10-19 真机踩过）。`tests/cfopt_test.py` 的 **H 组**专门钉死这条闭环。
- 后台刷新的子进程是**独立进程**（杀守护进程不会连带杀掉它）。停止应用时会先立
  「正在停止」标记、再显式回收它，防止它拿着上一轮结果把刚拉取的新配置覆盖回去；
  每次 `start` 的第一步也会回收一次，覆盖守护进程崩溃 / 被 `kill -9` 的残留。
- **升级应用后请重启一次**：识别旧守护进程靠命令行的脚本路径，升级会换安装目录；
  匹配已放宽到「同名 `<...>/cmd/<脚本名>`」，但最稳妥的仍是一次 stop + start。

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
