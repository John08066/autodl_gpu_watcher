# AutoDL GPU Watcher v0.6.3

用于实验室 AutoDL 私有云 GPU 资源监控、占用统计和满足条件后的自动开机。

## v0.6 桌面界面

在 Git 仓库目录双击 **`START_HERE.vbs`**，只打开桌面 UI，不显示额外 CMD/PowerShell 黑框。启动失败会弹出错误提示，详细信息保存在 `.ui/startup.log`。运行日志和占用报表默认隐藏，勾选“显示辅助工具”后才显示。

`START_HERE.cmd` 默认转交无控制台入口并退出，但双击 CMD 本身仍可能瞬间闪出终端，因此日常请使用 `.vbs`。`START_HERE.cmd --cli` 保留需要终端交互的旧菜单。启动器仍优先加载本仓库的 `src/`。

1. 点击“刷新服务器”，获取当前 AutoDL 账号的实际入口和空闲 GPU ID。尚未刷新时仅显示配置中的入口，不代表当前在线。
2. 登录失效时点击“登录 / 更新会话”，在 Edge 完成验证码登录并关闭登录窗口，再点击“已登录并关闭浏览器”，随后重新刷新。
3. 点击一个服务器入口，填写“本人用户名”。这是“查看占用”中的显示名，不会切换 AutoDL 登录账号，也不是 SSH 用户名。
4. 设置采样间隔与占用采集间隔，支持有限正数和小数。占用采集在监控循环内调度；网络请求耗时或较长的采样间隔会使实际占用采集间隔变长，不会并发堆积请求。
5. 点击“开始监控”。默认只读，不发送开机请求。最近一次成功采样状态始终显示；需要查看连续输出或导出占用报表时，勾选“显示辅助工具”。
6. 需要自动开机时，先停止监控、勾选“启用真实自动开机”，再启动。所选入口必须在 `config.yaml` 的 `auto_start.targets` 中配置有效实例 UUID；新发现但未配置实例的入口仍可只读监控。
7. 点击“停止监控”后等待当前网络请求结束，程序保存状态并关闭采集浏览器。切换服务器或配置需要先停止；关闭窗口也会等待安全停止。

“保存设置”只写入本地 `.ui/preferences.json`，不会改写受 Git 管理的 `config.yaml`。记住入口、用户名和间隔，每次重新打开 UI 都默认只读。登录资料、日志和占用数据库位于项目内 `runtime/`，但整体被 Git 忽略。同一套浏览器资料应只运行一个监控程序。

“导出占用报表”保留原来的 SQLite → CSV 导出流程，但仅在勾选“显示辅助工具”后出现。UI 登录失效时结束当前监控并提示登录；完成登录后重新开始，命令行仍保留原有交互式登录恢复。

### 命令行与开发

保留旧菜单：运行 `START_HERE.cmd --cli`。也可在安装项目的 Python 环境内直接运行：

```powershell
python -m autodl_watcher.gui
python run_watcher.py --host 203 --entry 2 --user "你的占用用户名" --poll-seconds 5 --usage-seconds 30 --dry-run
```

需要安装时运行 `tools\install_update.cmd`；首次创建环境及依赖可执行 `python -m pip install -e .`，系统需有 Microsoft Edge 和 Python 的 Tkinter 支持。

`main.main()` 负责配置和参数解析，`run_monitor()` 负责原有采集、评估、占用确认、通知、开机和持久化。`gui.py` 用独立子进程运行这些逻辑，通过消息队列把输出送回 Tk 主线程，通过停止文件请求安全退出。网络请求不阻塞 UI。

v0.6 将 Python 中重复的多行说明改为简短行尾注释，并压缩可读的短调用；复杂条件、SQL 和浏览器脚本保留必要换行。

## 目录结构

根目录只保留日常会直接接触的文件；辅助启动脚本统一放进 `tools/`：

```text
autodl_gpu_watcher_git/
├─ src/                  Python 主代码（仅保留 UI、监控、采集、开机、统计和通知链路）
├─ tests/                单元测试
├─ tools/                当前 UI/命令行会调用的辅助脚本
├─ START_HERE.vbs        日常双击入口，无额外控制台窗口
├─ START_HERE.cmd        兼容入口；--cli 打开旧菜单
├─ README.md             使用说明
├─ CHANGELOG.md          版本迭代记录
├─ config.yaml           配置文件
├─ pyproject.toml        Python 项目配置
├─ run_watcher.py        Python 直接启动入口
├─ .env.example
└─ .gitignore
```

`src/autodl_watcher/` 已移除早期的模拟演示、单独的手动开机演示、旧 CSV 导入器和独立冒烟脚本；实时刷新、只读/真实监控、登录、报表与 Edge 清理均保留在日常入口中。旧版本源码和迁移工具仍可从 Git 的历史标签查看。

正常使用时只需要双击：

```text
START_HERE.vbs
```

## 兼容命令行菜单（START_HERE.cmd --cli）

```text
1. Login / refresh session
2. Start monitor - auto-select entry
3. Start monitor - 203-2 only
4. Dry run - no automatic power-on
5. View live log
6. Export usage report
7. Clean watcher Edge processes
8. Install / update this version
0. Exit
```

第一次解压新版本：

```text
8  安装/更新当前版本
1  重新登录一次
3  固定监控 203-2
```

以后通常直接按 `2` 或 `3`。

## v0.5.4 关键修复

### A. 修复 203-1 / 203-2 占用串读

占用采集现在必须同时满足：

```text
弹窗标题 = 占用详情
AND
弹窗正文明确包含当前入口 machine_name
AND
只解析该弹窗内部表格
AND
machine/list 的 idle/total 与弹窗占用数一致
```

任一条件不满足，本轮直接显示“采集失败”，绝不拿另一入口的数据顶上。

### B. 误判关机改为连续确认

一次空占用详情只记为：

```text
本人状态[疑似结束 1/2，待复核]
```

随后快速复核；只有连续两次可靠空快照才执行：

```text
ACTIVE -> ABSENT -> 清 alerted -> 自动开机重新武装
```

采集失败不会推进确认次数。第一次疑似空快照也不会在 SQLite 中生成 `END_SEEN`。

### C. AutoDL 控制面默认绕过系统代理

该 PC 实测开启 Clash 系统代理时 `private.autodl.com` 明显更慢，而关闭代理后恢复。
v0.5.4 因此把 watcher 自己访问 AutoDL 的流量设为直连：

```text
private.autodl.com / *.autodl.com -> DIRECT
watchgpu.vpms-lab.com            -> 仍可走本机 Clash 7897
```

这只影响 watcher 的专用登录 Edge / 后台 Edge，不会修改你普通 Edge 的全局 Clash 配置。

### D. 固定 203-2 没空位时不再假触发

如果 203-1 有空位、但固定目标 203-2 没空位，终端只会显示：

```text
固定入口当前无可用实例，继续等待
```

不会再每 10 秒刷“没有可用固定实例”。



### 0. 修复“已登录却反复判定登录失效”

v0.5.1 把“20 秒内没有捕获到 `/api/v2/machine/list` 响应”直接当成登录失效。
这会在 AutoDL 页面/API 很慢时反复弹登录窗口。

v0.5.3 改为明确区分：

```text
/login 或 HTTP 401/403
    -> 确认登录失效，才弹普通 Edge

接口超时 / 429 / 5xx / 页面响应慢
    -> 平台瞬时故障
    -> 本轮跳过开机
    -> 保持当前登录状态并自动重试
```

### 0.1 主机列表改为“首次浏览器捕获，后续直接 API”

首次启动仍由 Playwright 打开 AutoDL 控制台并捕获：

```text
Authorization + machine/list 请求体
```

捕获成功后，后续轮询优先直接请求 `machine/list`，不再每 10 秒整页刷新。
这样可以减少：

- AutoDL 页面一直转圈；
- 页面刷新过慢；
- 重复加载前端资源；
- 因响应慢而误判登录失效。

如果直接 API 暂时超时，会按配置重试；仍失败时只跳过本轮，不触发重新登录。

### 0.2 登录恢复必须二次验证

人工登录结束后不再立刻打印“登录恢复完成”。
现在流程是：

```text
人工登录并同步 profile
-> 下一轮真实 machine/list 成功
-> 才打印“登录验证成功，平台主机接口已恢复”
```

### 1. 彻底取消 SQLite 对“本人已开机”的参与

`occupancy.db/current_instances` 现在只用于历史统计，不再用于当前开机状态判断。

程序每次启动时本人状态固定从：

```text
UNKNOWN
```

开始。只有本进程实时“查看占用”明确看到配置的本人用户名，才会显示绿色：

```text
已占用 / 已开机
```

因此不会再出现“数据库里残留旧记录 → 实际没开机 → 终端却一直绿色”的情况。

### 2. 启动前先确认本人是否已经占用

程序刚启动、或者会话刚恢复时，如果还没有成功采到完整占用详情：

```text
开机达标=本人状态未知
动作=暂缓开机，先确认本人占用
```

这时不会贸然再开第二个实例。

完整快照确认本人不在后，下一轮才允许真正自动开机。

### 3. 被 K / 关机后重新武装

实时占用状态从：

```text
ACTIVE -> ABSENT
```

时，立即清除该主机 evaluator 的 `alerted` 状态：

```text
自动开机=重新武装
```

后续平台空位和显存再次达标即可重新开机。

### 4. power_on Success 不再冒充“已开机”

AutoDL 返回 `Success` 只代表平台受理开机请求。

v0.5.1 会进入最长 120 秒的：

```text
请求已受理 -> 等待实例出现在占用详情
```

只有占用详情真实看到本人实例才变绿色。

120 秒仍看不到本人实例时，自动重新武装，不会永久锁死。

### 5. PC 登录与监控浏览器进一步隔离

人工验证码改用全新的：

```text
runtime/native_login_profile
```

后台 Playwright 继续使用：

```text
runtime/browser_profile
```

人工登录窗口由系统 Microsoft Edge 直接启动，不使用 Playwright，不带：

```text
--no-sandbox
--headless
--remote-debugging-pipe
```

登录前、同步前会主动等待旧 Edge 完全退出并清理 `Singleton*` / `DevToolsActivePort` 等残留锁文件。

> 这修复的是脚本自身导致的验证码高风险浏览器环境。AutoDL 平台自身仍可能根据账号/IP/设备风控要求验证码，工具不会自动破解验证码。

### 6. 修复菜单 7 的 PowerShell 报错

v0.5.0 的菜单 7 把 CMD 转义符 `^` 错传给 PowerShell，导致：

```text
Get-CimInstance : 找不到接受实际参数“^”的位置形式参数
```

v0.5.1 已把清理逻辑独立到：

```text
tools/clean_watcher_edge.ps1
```

不再使用错误的 `^|` 管道写法；PID 已提前退出时也静默忽略。

### 7. 后台 Edge profile 锁自动自愈

后台 Playwright 启动前会主动清理 watcher 专用 `browser_profile` 的残留 Edge 与锁文件。

因此登录完成后通常不再需要手工先执行菜单 `7` 才能启动监控。

## 自动开机判定

```text
平台对应入口存在空闲 GPU ID
AND
物理 GPU 可用显存 >= max(8192 MB, 总显存 × 25%)
AND
本人占用状态已实时确认不是 ACTIVE
```

GPU Util 默认只记录，不作为硬门槛。

## 运行数据

项目内运行数据：

```text
D:\Dev\MCP\autodl_gpu_watcher_git\runtime\
```

主要内容：

```text
logs/watcher.log
state.json
usage/occupancy.db
usage/exports/
native_login_profile/
browser_profile/
```

## 常见操作

安全停止监控：

```text
Ctrl+C
```

窗口标题出现“选择”时：

```text
按 Esc
```

传统 PowerShell/CMD 进入选择模式会暂停前台 Python。

查看原始日志与导出占用报表均属于辅助功能：在桌面 UI 勾选“显示辅助工具”后使用；命令行菜单入口仍保留。

会话过期时，主程序会停止后台 Playwright并弹出普通 Edge；完成验证码和登录后按 Enter，原监控进程继续运行。

## 测试

v0.6.1：合并原误标为 0.61 的无控制台启动更新与代码注释更新，保留用户调整的 `1000x800` 窗口尺寸。无控制台功能此前通过 88 项测试及实际服务器刷新、底部日志和只读监控启停验证。

阅读源码时可按以下顺序理解：`tools/gui_entry.py` 启动窗口 → `gui.py` 读取设置并管理后台进程 → `main.py` 加载配置、组织每轮监控 → `collectors/platform.py` 与 `collectors/telemetry.py` 分别采集平台空位和物理 GPU 数据 → `evaluator.py` 判断连续达标 → `autostart.py` 复核并请求开机。占用详情同时交给 `usage.py` 记录，`usage_report.py` 导出统计，`state_store.py` 保存连续采样状态。各模块的关键状态、时间基准和数据含义已补充中文行尾注释。

```powershell
python -m unittest discover -s tests -v
```

v0.6：86 项自动化测试通过，包括 Tk 窗口交互和 Windows CMD 入口测试。已实测读取 5 个入口，并对 203-1、202-4 各完成三轮只读监听；未实测发送真实开机请求或邮件。

202 主机的占用表存在比平台 total 更多的空行。只有新鲜 Telemetry 索引与 total 一致、额外行没有占用/实例/用户证据时才排除这些行；其余不一致仍会拒绝，避免错误确认本人已下机。

完整版本变化见同级 [`CHANGELOG.md`](CHANGELOG.md)。
