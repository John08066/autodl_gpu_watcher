# Changelog

## v0.6.2 — 全项目死代码清理

发布日期：2026-09-08

- 审阅全部运行代码、启动脚本、测试和文档引用；保留监控安全门控、登录恢复、占用确认、自动开机、报表、安装和 Edge 清理链路。
- 删除无产品入口引用的模拟 evaluator 演示、独立真实采集 smoke、手工开机演示和仅服务于 v0.4.2 及以前 CSV 的导入器；不删除任何已有运行数据，历史实现仍可从 Git 标签取得。
- 删除仅为上述模块存在的控制台通知器、`select_cli_target()`、Telemetry 旧过滤包装、SQLite 旧类/函数别名及未使用导入；`main.py` 不再保留两层过滤后未使用的元组变量。
- 启动器不再探测相邻旧版本 `v0.4.7` / `v0.4.9` 的虚拟环境，只使用当前项目 `.venv` 或标准 Conda 环境，避免错误启动旧代码。
- 验证：全部 Python 文件 AST 语法解析、全量自动化测试和 `git diff --check` 通过。

## v0.6.1 — 无控制台桌面启动与源码注释

发布日期：2026-09-08

- 版本号统一为 `0.6.1`，原误标 `0.61` 的启动更新与本次注释更新合并为一个版本、一次提交。
- 保留用户在 `gui.py` 中调整的 `1000x800` 窗口尺寸，底部日志随窗口布局扩展。
- 补充中文行尾注释，覆盖界面与子进程通信、配置加载、平台/物理 GPU 采集、连续达标、本人占用确认、开机复核、SQLite 事件及报表统计；README 增加源码阅读路线。
- 注释更新验证：14 个 Python 文件新增 218 条注释；37 个 Python 文件与修改前基准的 AST 一致（仅归一化版本号），确认保留 `1000x800`，88 项回归测试通过，`git diff --check` 通过。本次未重复进行真实服务器监听测试。
- 新增日常双击入口 `START_HERE.vbs`：隐藏环境准备窗口，使用 `pythonw.exe` 打开主界面，不再保留额外的 CMD 黑框。
- `START_HERE.cmd` 默认转交新入口后退出；显式传入 `--cli` 时继续打开原命令行菜单。要避免 CMD 本身短暂闪现，请直接双击 `.vbs`。
- GUI 的后台任务仍使用 `python.exe` 配合 `CREATE_NO_WINDOW` 和输入/输出管道，刷新、监控、登录、导出日志继续显示在主界面底部。
- 登录资料检查和清理所用的 PowerShell 辅助进程改为无窗口运行；需要用户操作的 Edge 登录窗口照常显示。
- 无控制台启动异常写入 `.ui/startup.log` 并弹窗提示，避免依赖缺失或配置错误时静默退出。
- 保留 v0.6 的服务器选择、可配置用户名/间隔、占用校验和安全停止功能；本次补注释不调整业务逻辑，v0.6 更新摘要及更早记录保留在下方。
- 验证：88 项自动化测试通过，覆盖带空格路径的 VBS → CMD → pythonw 启动链、启动异常提示和日志管道；实际刷新、只读监听及安全停止通过，正式入口仅主界面可见，后台任务未检测到可见终端窗口。

## v0.6 — 桌面 UI、可配置监控与紧凑注释

发布日期：2026-09-07

### 界面与配置

- 双击 `START_HERE.cmd` 默认打开 Tkinter 桌面界面，`START_HERE.cmd --cli` 保留旧命令行菜单。
- 从当前账号实际主机列表刷新服务器入口、平台空闲 GPU ID 和开机配置状态，点击选择监控目标。
- 支持修改占用识别用户名、采样间隔和占用采集间隔；间隔支持小数，拒绝零、负数、NaN 和无穷大。
- 设置保存在 Git 忽略的 `.ui/preferences.json`；不覆盖原始 YAML 配置，每次打开界面默认只读。
- 支持登录、实时日志、启动/停止监控、占用报表导出。未配置实例 UUID 的新入口可以只读监控，禁止直接真实开机。

### 结构与兼容

- 配置解析入口与 `run_monitor()` 分离；UI 用子进程复用原有采集、评估、占用确认、自动开机、通知和持久化流程。
- 通过消息队列在 Tk 主线程更新控件；通过停止文件请求安全退出，在采集步骤之间检查停止信号，保存状态并关闭浏览器和日志文件。
- 保留命令行交互式登录恢复；UI 登录失效时退出监控并提示用户完成登录后重新开始。
- 修复启动 CMD 中 `if ... & exit` 导致成功加载环境后仍提前退出的问题；启动器显式优先加载当前仓库 `src/`，避免误运行旧安装版本。
- Python 多行说明精简为行尾注释，短调用适度合并；复杂条件、SQL 和浏览器脚本保留必要换行。未调整业务的模块通过去除文档字符串后的 AST 等价检查。

### 实测发现与修复

- 202 主机的 `machine/list` 显示 3 张可分配 GPU，但占用弹窗包含 4 行，旧版严格行数校验会拒绝该快照。
- 仅当新鲜 Telemetry 的唯一 GPU 索引数量等于平台 total、占用表包含这些索引、额外行没有占用标记/实例/用户时，才排除额外空行。缺少佐证或额外行有占用证据时继续拒绝；入口、重复索引和占用数量校验保持生效。
- 实际读取 5 个入口；203-1 和新发现的 202-4 各完成三轮只读监听，覆盖平台空位、物理 GPU、占用快照、状态保存和退出。分别使用 5 秒和 3 秒采样设置；网络及占用采集耗时会影响实际周期。
- 测试使用隔离日志和数据库，未发送真实开机请求；真实自动开机和邮件发送未进行外部实测。

### 验证

- 86 项自动化测试通过，包含原有 70 项及 UI 参数、窗口交互、启停、核心循环、会话失效、额外 GPU 空行和 CMD 启动入口回归。
- 桌面窗口布局检查、源码 AST 对比和 `git diff --check` 通过。
- 完整用法见 `README.md`；历史版本 v0.5.0～v0.5.4 的提交和标签保留。

## v0.5.4 — 占用串读/误判关机与 AutoDL 代理绕行修复

发布日期：2026-08-09

### 修复：203-2 数据串到 203-1

- `collect_occupancy()` 不再扫描整页 `tr:visible`。
- 只解析同时包含“占用详情”、目标 `machine_name`、`GPU INDEX` 和“是否被占用”的最小可见弹窗容器。
- 切换入口前先关闭旧弹窗；采集完成后等待当前弹窗真正隐藏，再采下一个入口。
- 每个入口的占用结果与 `machine/list` 的 `idle/total` 做一致性校验。
- 若 203-1 平台显示 `2/2` 空闲，却读到 2 张占用卡，直接判本轮采集失败，不再把 203-2 的数据贴到 203-1。

### 修复：一次空弹窗误判本人已关机

- `ACTIVE -> ABSENT` 不再由单次空快照触发。
- 默认要求连续 2 次“全部入口采集成功 + 本人均不存在”才确认关机。
- 第一次疑似下机后 5 秒进入快速复核，不再等待完整 60 秒。
- 第一次空快照不会让 SQLite 生成 `END_SEEN`，避免历史统计产生伪下机/伪上机。
- 任一入口采集失败会打断空快照连续计数，保持原本人状态。

### 修复：固定 203-2 无空位时反复刷“没有可用固定实例”

- evaluator 聚合层即使因为 203-1 有空位生成事件，固定入口 203-2 当前无可用实例时也会在主循环层吃掉该事件。
- 终端改为“固定入口当前无可用实例，继续等待”，不再每 10 秒调用无意义的自动开机尝试。

### 网络改进：AutoDL 控制面绕过 Clash 系统代理

- 用户实测该 PC 关闭 Clash 系统代理后 `private.autodl.com` 明显恢复快速，说明系统代理路径是主要慢点。
- watcher 的后台 Edge 和人工登录 Edge 默认对 `private.autodl.com` / `*.autodl.com` 使用 DIRECT bypass。
- `NO_PROXY` 同步加入 AutoDL 域名；BrowserContext 及关联 APIRequestContext 采用同一 bypass 策略。
- Telemetry 仍可使用 `127.0.0.1:7897`，因此不影响 `watchgpu.vpms-lab.com` 的代理需求。
- launcher 只在本机 7897 端口实际监听时才注入 `HTTP_PROXY/HTTPS_PROXY`，新电脑没有 Clash 时不会被一个不存在的代理端口拖死。

### 配置新增

- `platform.autodl_direct: true`
- `platform.proxy_bypass_list`
- `usage_tracking.absent_confirmations_required: 2`
- `usage_tracking.absence_recheck_seconds: 5`

### 回归测试

- 70 项单元测试通过。
- 新增覆盖：跨入口占用串读拒绝、平台 idle/total 与弹窗矛盾拒绝、单次空快照不确认关机、第二次连续空快照才确认、采集失败打断空快照计数、launcher AutoDL NO_PROXY。

## v0.5.3 — Playwright APIResponse 兼容性修复

- 修复直接调用 `machine/list` API 后持续报错：`'APIResponse' object has no attribute 'request'`。
- 根因：浏览器页面的 `Response` 有 `.request`，但 `context.request.post()` 返回的 `APIResponse` 没有该属性；v0.5.2 把两种响应对象当成了同一种。
- 现在解析器仅在响应对象确实带有 `.request` 时才读取请求元数据；直接 API 路径继续复用内存中的 Authorization 与请求体。
- 保留 v0.5.2 的认证状态机：只有 `/login` 或 HTTP 401/403 才判定登录失效，普通页面/API 超时仍按瞬时采集故障处理。
- 新增回归测试，模拟没有 `.request` 属性的 Playwright `APIResponse`，确保直接 API 快路径可以连续运行。

# Version History / 版本迭代记录

## v0.5.2 — 平台接口慢响应与登录误判修复

发布日期：2026-08-09

### 修复：已登录却反复弹登录窗口

- 删除“20 秒没捕获到 machine/list 响应 = 登录失效”的错误判定。
- 只有以下明确证据才触发人工登录恢复：
  - 当前页面进入 `/login`；
  - machine/list / AutoDL API 返回 HTTP 401 或 403。
- 页面仍停留在 `/console/machine` 时，API 超时、429、5xx 全部视为瞬时采集故障。
- 瞬时故障只跳过本轮自动开机，保持登录状态并继续下一轮，不再重复弹 Edge。

### 改进：主机列表不再每 10 秒整页刷新

- Playwright 首次捕获 machine/list 请求时，立即在内存保存 Authorization 与请求体。
- 后续轮询优先通过 BrowserContext APIRequestContext 直接 POST machine/list。
- 减少 AutoDL 前端页面频繁 reload、长时间转圈和前端资源重复加载。
- direct API 支持瞬时失败重试，配置项：
  - `platform.max_attempts`；
  - `platform.retry_delay_seconds`。

### 修复：人工登录后的假“恢复成功”

- 人工登录并同步 profile 后只提示“登录资料已更新，等待验证”。
- 必须等下一轮真实 machine/list 成功后，才打印“登录验证成功，平台主机接口已恢复”。

### 修复：未捕获 Authorization 不再误判登录失效

- 没捕获到 Authorization 但没有 `/login` / 401 / 403 时，改为瞬时平台故障。
- 避免已登录页面因为网络抖动再次被送去验证码。

### 保留 v0.5.1 状态机修复

- SQLite 历史记录不参与本人实时开机状态。
- ACTIVE -> ABSENT 后重新武装。
- power_on Success 仅表示请求受理，等待占用详情确认。
- 人工登录与 Playwright profile 隔离。
- watcher Edge/profile 锁自动清理。

### 回归测试

- 61 项单元测试通过。
- 新增覆盖：控制台超时不判认证失败、登录页超时才判认证失败、请求监听提前捕获 token/请求体、缓存请求上下文后不再整页 reload。

---

## v0.5.1 — 目录整理、状态机收口与登录/清理修复

发布日期：2026-08-09

### 目录整理

- 根目录只保留 `START_HERE.cmd` 一个日常启动脚本。
- `launcher/install/export/log/Edge cleanup` 等辅助脚本统一移动到 `tools/`。
- 不再把空的 `runtime/` 打进发布包；所有版本继续共用项目同级 `autodl_watcher_runtime/`。
- `README.md` 与 `CHANGELOG.md` 保持同级。

### 修复：SQLite 历史状态导致假绿色

- 完全取消 SQLite `current_instances` 对启动时本人占用状态的影响。
- 每个新进程都从 `UNKNOWN` 开始，只接受本进程实时占用快照作为 ACTIVE/ABSENT 事实源。
- 近期 evaluator 状态可以恢复连续采样，但恢复后无条件清除跨进程 `alerted` 锁。

### 修复：启动/会话恢复后先确认本人状态

- 本人占用状态未确认时，自动开机暂缓。
- 首个完整占用快照确认本人不在后才允许开机，避免重复启动第二个实例。
- 部分入口采集失败仍保持 UNKNOWN，不会误判 ACTIVE 或 ABSENT。

### 修复：power_on 受理与真实开机分离

- `request_accepted` 不再改变本人状态。
- 新增 120 秒占用确认宽限期。
- 宽限期内不重复发送开机请求。
- 超时仍未看到本人实例时自动重新武装。
- 请求未真正受理时清除本轮 evaluator 去重锁，允许后续重试。

### 修复：被 K / 关机后的重新武装

- 实时状态明确从 ACTIVE 变为 ABSENT 时立即执行 `rearm_host()`。
- 旧数据库记录、旧 `alerted=True`、旧 request 状态均不能阻塞后续自动开机。

### 修复：PC 人工登录验证码环境

- 人工登录 profile 改名为全新的 `native_login_profile`，主动避开旧版可能污染的 `login_profile`。
- 普通 Edge 登录前后都会等待专用 Edge 完全退出。
- 自动清理 `Singleton*`、`lockfile`、`DevToolsActivePort` 等 profile 锁。
- 检测到人工登录 Edge 带 `--no-sandbox` / `--headless` / `--remote-debugging-pipe` 时主动停止，而不是让用户继续在高风险环境里反复验证。

### 修复：后台 Playwright profile 锁

- `PlatformBrowserCollector.start()` 启动前主动释放 `browser_profile` 的残留 Edge/锁文件。
- 保留 v0.5.0 的异常安全 Playwright 清理，避免首次启动失败后永久刷 `Sync API inside the asyncio loop`。

### 修复：菜单 7

- 删除 v0.5.0 `CLEAN_WATCHER_EDGE.cmd` 中错误的 CMD `^|` → PowerShell 管道转义。
- 清理逻辑改为独立 `tools/clean_watcher_edge.ps1`。
- PID 已提前退出时静默忽略；最后重新检查是否仍存在 watcher Edge。

### 回归测试

- 57 项单元测试通过；覆盖状态恢复、登录 profile 隔离、Playwright 启动失败清理、SQLite 部分快照、发布目录与菜单脚本。

---

## v0.5.0 — 状态可信度与登录隔离重构

发布日期：2026-08-09

### 修复：假绿色“已开机”

- SQLite `current_instances` 不再直接当作当前实时占用事实。
- 启动时数据库历史占用仅作为短时保护提示，不会直接令 `self_active=True`。
- 只有当前进程实时占用快照明确看到 `usage_tracking.self_user`，才显示绿色“已开机”。
- `power_on` 返回 `Success` 只表示请求已受理，不再创建伪 `owned_instances`。
- evaluator 持久化状态增加时效限制：只恢复 5 分钟内状态；陈旧 `alerted=True` 不再跨长时间停机锁死自动开机。
- 近期 SQLite 占用提示最多保护 3 分钟；始终无法确认时会重新武装 evaluator，避免永久停在“已处理”。

### 修复：被 K / 关机后的重新武装

- 所有目标入口完整采集成功且本人从“确认占用”变为“不占用”时，清除目标主机全部 `alerted`。
- 启动时存在近期历史占用提示，但完整快照确认本人已不在时，同样主动重新武装。
- 部分入口失败时不会误判本人下机。

### 修复：部分占用采集污染 SQLite

- `UsageSqliteLogger.record()` 新增 `complete_snapshot` 与 `snapshot_hosts`。
- 部分入口采集失败时只写快照，不修改 `current_instances`，不生成 `END_SEEN`。
- 完整快照只清理本次覆盖的物理 host，不再误伤数据库中其他 host 的 current state。

### 修复：PC 登录验证码频繁失败

- 新增独立 `runtime/login_profile`，只供普通 Microsoft Edge 人工登录。
- Playwright 只使用 `runtime/browser_profile`，不再把人工验证码页面置于后台自动化 profile 中。
- 登录前自动清理两个专用 profile 的残留 Edge 进程。
- 登录 Edge 启动后检测 `--no-sandbox` / `--remote-debugging-pipe` / `--headless` 污染；发现后自动清理并重试一次。
- 登录完成后自动把干净 `login_profile` 同步到 `browser_profile`。
- Windows 找不到系统 Edge 时不再回退到 Playwright 做验证码登录，避免再次进入高风险自动化环境。

### 新增：会话失效自动恢复

- 主循环捕获 `PlatformAuthenticationError` 后自动关闭 Playwright。
- 自动播放 Windows 提示音并弹出普通 Edge 登录窗口。
- 用户完成验证码、关闭 Edge 并按 Enter 后，当前监控进程自动继续，不需要重新启动脚本。

### 修复：Playwright profile 锁后的永久 Sync API 错误

- `PlatformBrowserCollector.start()` 在浏览器启动失败时彻底释放 Playwright/context。
- `close()` 改为异常安全清理。
- 下一轮能够重新启动，不再永久刷 `Sync API inside the asyncio loop`。

### 改进：占用页面兼容性

- “查看占用”定位同时支持旧表格 `<tr>` 和新 card/div 结构。

### 工具

- 根目录内置 `START_HERE.cmd` 一键菜单。
- Python 启动器自动兼容：项目内 `.venv`、旧 v0.4.7 `.venv`、主机 Conda `autodl-watcher` 环境。
- Edge 清理菜单静默忽略“PID 已提前退出”的正常竞争，不再打印大量红色 `Stop-Process` 错误。
- 新增 `INSTALL_UPDATE.cmd`。

### 回归测试

- 51 项单元测试通过。
- 新增：陈旧 evaluator 状态、陈旧 SQLite 占用、部分快照不产生伪 END、登录 profile 隔离、Playwright 启动失败清理等测试。

---

## v0.4.9 — Telemetry 抗抖动

- Telemetry 单次超时提高到 20 秒。
- 瞬时失败后等待 2 秒重试一次。
- HTTP 429、5xx、ReadTimeout、ConnectionError 纳入重试。

## v0.4.8 — 普通 Edge 人工登录

- Windows 登录优先直接启动系统 Edge。
- 解决 Playwright 登录窗口更易触发验证码的问题。
- 后续发现登录与监控仍共用 `browser_profile`，在残留 Playwright Edge 时仍可能复用自动化进程；v0.5.0 已通过双 profile 彻底隔离。

## v0.4.7 — 被 K / 关机后的重新武装

- 完整占用快照确认本人实例结束后清除目标主机 `alerted`。
- 允许资源重新满足条件后再次自动开机。

## v0.4.6 — 本人占用识别与绿色状态

- 增加 `usage_tracking.self_user`。
- 本人正在占用时显示绿色状态并阻止第二入口重复开机。

## v0.4.5 — 注释增强

- Python 函数/方法增加结构化中文 docstring。
- 自动开机触发与成功状态增加终端颜色。

## v0.4.4 — SQLite 占用数据库

- 实时占用从 CSV 改为 SQLite，避免 WPS/Excel 文件锁。
- 支持同一物理 GPU 上多个实例/用户并发统计。
- 上下机事件按 instance_id 追踪。
