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
