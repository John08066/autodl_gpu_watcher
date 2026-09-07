# AutoDL GPU Watcher v0.5.4

用于实验室 AutoDL 私有云 GPU 资源监控、占用统计和满足条件后的自动开机。

## 目录结构

根目录只保留日常会直接接触的文件；辅助启动脚本统一放进 `tools/`：

```text
autodl_gpu_watcher_v0.5.4/
├─ src/                  Python 主代码
├─ tests/                单元测试
├─ tools/                一键菜单使用的辅助脚本
├─ START_HERE.cmd        日常唯一入口
├─ README.md             使用说明
├─ CHANGELOG.md          版本迭代记录
├─ config.yaml           配置文件
├─ pyproject.toml        Python 项目配置
├─ run_watcher.py        Python 直接启动入口
├─ .env.example
└─ .gitignore
```

正常使用时只需要双击：

```text
START_HERE.cmd
```

## 一键菜单

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

开始。只有本进程实时“查看占用”明确看到 `何太急`，才会显示绿色：

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
../autodl_watcher_runtime/native_login_profile
```

后台 Playwright 继续使用：

```text
../autodl_watcher_runtime/browser_profile
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

所有版本共用：

```text
D:\Dev\MCP\autodl_watcher_runtime\
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

查看原始日志：菜单 `5`。

导出占用报表：菜单 `6`。

会话过期时，主程序会停止后台 Playwright并弹出普通 Edge；完成验证码和登录后按 Enter，原监控进程继续运行。

## 测试

```powershell
python -m unittest discover -s tests -v
```

v0.5.4 发布前回归测试：70 项通过。

完整版本变化见同级 [`CHANGELOG.md`](CHANGELOG.md)。
