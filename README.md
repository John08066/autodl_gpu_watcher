# AutoDL GPU Watcher v0.6.8

实验室 AutoDL 私有云 GPU 监控、占用统计及满足条件后的自动开机。

## Windows EXE

打开 `dist/AutoDLWatcher/AutoDLWatcher.exe`，没有额外终端窗口。发行目录只需 EXE、`_internal/` 依赖和 `config.yaml`；不需要安装 Python，但需要系统 Microsoft Edge。迁移到另一台机器时解压干净包 `dist/AutoDLWatcher-v0.6.8.zip`，再自行登录。不要发送含个人会话的 `runtime/` 或 `.ui/`。

1. 点击“登录 / 更新会话”，在 Edge 完成登录并关闭浏览器，再点击“已登录并关闭浏览器”。登录只更新会话，不会自动开始监控。
2. 点击“刷新服务器”，选择入口，设置本人用户名、采样间隔和占用采集间隔。用户名用于旧弹窗识别；新版弹窗以当前账号完整实例列表核对身份。
3. 点击“开始监控”。每次打开窗口默认只读；底部实时日志默认显示，成功绿色、失败及断连红色。服务器表格是刷新时的快照，下方日志和状态行是持续监控结果。
4. 需要真实开机时勾选“启用真实自动开机”。若允许中断正在运行的无卡实例，再额外勾选“空闲时将所选无卡实例关机并改为有卡开机”。这两个开关不会保存为下次默认值。
5. 点击“停止监控”或关闭窗口，等待当前请求结束并保存状态。关机后停止监控可能使实例保持关机。

`config.yaml` 的 `auto_start.targets` 必须填写当前账号真实实例的完整 UUID 和入口名；重建实例后旧 UUID 不能继续使用。尚未配置实例的入口仅支持只读监控。不要把修改占用用户名误当作切换 AutoDL 登录账号。

无卡转换会确认同一 UUID 正在 `running/non_gpu`，核对目标入口空闲 GPU INDEX 与新鲜显存达标 INDEX 的交集，发送关机，逐轮等待 `shutdown`，重新核对容量和实例，再发送 `start_mode=gpu`。容量变化、身份不明或停止信号会阻断下一步。普通真实有卡开机也必须逐卡核对。

平台空位与用户GPU额度是两项独立限制。该平台无卡运行也会占用有卡启动的数量额度；本次201已关机而203无卡运行时仍返回“上限1、已使用1”，获准关闭203后201有卡启动成功。仅关闭所选无卡实例不一定释放所有额度，程序不会自动关闭其他入口的实例。已确认额度错误 `GpuStockReqNum` 后，当前监控进程不再发送电源请求；先处理额度，再停止并重新开始监控。当前没有已验证的额度预检接口，因此切换仍可能被额度或容量竞争拒绝；接口受理也不等于实例已运行。开机API未指定物理GPU INDEX，实际卡由平台分配，本次实际索引核对通过不保证以后每次分配都与预检相同。

## 本次验证范围

- 201-1 / A100：2026-09-26完整实测通过。关闭获准中断的203-1释放额度后，201-1无卡启动；监控程序触发同一UUID关机，确认shutdown，再有卡开机；最终running/gpu，实际分配GPU #2与开机前达标空闲INDEX一致。
- 202 / TITAN RTX：真实只读监控通过，未执行电源操作；203 / V100：只读采集、真实关机及恢复无卡运行通过，未进行有卡开机验收。
- EXE 在无外部 Python PATH 下完成三轮201真实只读采集。148 项源码回归及打包测试通过，覆盖故障恢复、索引错配、响应丢失、额度拒绝不重复请求、隐藏后台管道和配置保留。
- 验收后201-1已关机，203-1原UUID已恢复无卡运行，203-2未操作。

## 源码开发与打包

```powershell
python -m pip install -e .
python tools/gui_entry.py
python run_watcher.py --host 201 --entry 1 --dry-run
python -m pip install PyInstaller==6.22.3
python tools/build_exe.py
python -m unittest discover -s tests -q
```

构建先写入 `.tools/build/`，再合并生成的 EXE 和依赖到 `dist/AutoDLWatcher/`，保留已有配置及运行数据。发布包不包含登录会话、环境变量凭据或历史日志。开发工具中的 `gui_entry.py`、`worker_entry.py`、`build_exe.py` 均有实际用途；后台 EXE 仅由主界面调用。原 CMD/VBS 菜单和重复启动、安装、导出、日志脚本已被EXE及界面替代。

配置中的相对路径以配置文件目录为基准。`.ui/` 保存偏好和停止信号，`runtime/` 保存浏览器会话、状态、日志与占用数据库，均不纳入Git。每套浏览器资料同时只运行一个监控程序。`tools/clean_watcher_edge.ps1` 保留用于专用浏览器异常后的恢复。

项目特殊接口和探索结论参见 `Agent_MCP_Notes.md`；版本变化参见 `CHANGELOG.md`。

## 历史设计说明

以下记录旧版本的监控判定及修复背景；日常启动方式以本页EXE说明为准。

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

原始实时日志默认显示；导出占用报表在桌面 UI 勾选“显示辅助工具”后使用。旧命令行菜单已移除。

EXE 的后台监控使用 `--no-login`：会话过期会结束监控，请在主界面重新登录后再次开始。仅源码 CLI 交互模式会打开 Edge，完成登录并按 Enter 后继续监控。

## 测试

v0.6.1：合并原误标为 0.61 的无控制台启动更新与代码注释更新，保留用户调整的 `1000x800` 窗口尺寸。无控制台功能此前通过 88 项测试及实际服务器刷新、底部日志和只读监控启停验证。

阅读源码时可按以下顺序理解：`tools/gui_entry.py` 启动窗口 → `gui.py` 读取设置并管理后台进程 → `main.py` 加载配置、组织每轮监控 → `collectors/platform.py` 与 `collectors/telemetry.py` 分别采集平台空位和物理 GPU 数据 → `evaluator.py` 判断连续达标 → `autostart.py` 复核并请求开机。占用详情同时交给 `usage.py` 记录，`usage_report.py` 导出统计，`state_store.py` 保存连续采样状态。各模块的关键状态、时间基准和数据含义已补充中文行尾注释。

```powershell
python -m unittest discover -s tests -v
```

v0.6：86 项自动化测试通过，包括 Tk 窗口交互和 Windows CMD 入口测试。已实测读取 5 个入口，并对 203-1、202-4 各完成三轮只读监听；未实测发送真实开机请求或邮件。

202 主机的占用表存在比平台 total 更多的空行。只有新鲜 Telemetry 索引与 total 一致、额外行没有占用/实例/用户证据时才排除这些行；其余不一致仍会拒绝，避免错误确认本人已下机。

完整版本变化见同级 [`CHANGELOG.md`](CHANGELOG.md)。
