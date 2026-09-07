# AutoDL GPU Watcher v0.5.0

用于实验室 AutoDL 私有云 GPU 资源监控、占用统计和满足条件后的自动开机。

## 日常使用

Windows 下建议直接双击项目根目录的：

```text
START_HERE.cmd
```

菜单：

```text
1  Login / refresh session
2  Start monitor - auto-select entry
3  Start monitor - 203-2 only
4  Dry run - no automatic power-on
5  View live log
6  Export usage report
7  Clean watcher Edge processes
8  Install / update this version
0  Exit
```

第一次解压新版本，先按 `8` 安装当前版本，再按 `1` 登录；以后通常直接按 `2` 或 `3`。

## v0.5.0 关键修复

### 1. 不再把 SQLite 历史记录误判为“本人正在占用”

`current_instances` 只代表上一次成功看到的状态，不再作为实时真值。

- 启动时不会因为数据库里残留 `何太急` 就直接显示绿色“已开机”；
- 只有当前进程的实时“查看占用”结果明确看见本人实例，才进入绿色状态；
- 旧的 evaluator `alerted=True` 只在 5 分钟内允许恢复，避免放假/关机数小时后旧状态锁死；
- 近期 SQLite 占用仅作为 3 分钟启动保护提示，保护期过后会重新武装，不会永久阻止自动开机；
- `power_on` 返回 Success 只表示请求受理，不再立即伪装成“本人已开机”。

### 2. 部分占用采集失败不再制造伪下机事件

任一入口采集失败时：

- 仍保存已经成功采到的快照；
- 不更新 `current_instances`；
- 不生成伪 `END_SEEN`；
- 只有所有目标入口都成功，才允许确认“本人已经不再占用”并重新武装。

### 3. PC 登录验证码环境重做

人工登录改为两个 profile：

```text
runtime/login_profile     普通 Edge 人工登录专用，永不交给 Playwright
runtime/browser_profile   后台监控专用，Playwright 使用
```

流程：

```text
普通 Edge + login_profile 完成人工登录/验证码
→ 关闭 Edge 并按 Enter
→ 自动清理残留 Edge
→ 将干净登录 profile 同步到 browser_profile
→ 监控启动
```

这样人工验证码窗口不会复用后台 Playwright 的 `--no-sandbox`、`--headless`、`--remote-debugging-pipe` 环境。

> 验证码仍由用户本人完成。本工具不会自动破解或绕过验证码。平台自身的 IP/账号风控仍可能要求重新验证，因此不能承诺验证码 100% 永远通过。

### 4. 会话过期后自动进入登录恢复流程

监控中检测到 AutoDL 跳到 `/login` 后：

```text
关闭后台 Playwright
→ 自动弹普通 Edge
→ 提示音提醒
→ 用户完成验证码并关闭 Edge
→ 回终端按 Enter
→ 自动同步会话
→ 当前监控进程继续运行
```

不再需要 `Ctrl+C → 单独执行 login → 再重新启动 main`。

### 5. 修复 Playwright 首次启动失败后的永久报错

旧版遇到 profile 锁时，第一次 `launch_persistent_context` 失败后会残留 Playwright 状态，之后不断出现：

```text
It looks like you are using Playwright Sync API inside the asyncio loop.
```

v0.5.0 在启动失败时完整释放 context/playwright；下一轮可以正常重试。

### 6. “查看占用”兼容表格和卡片页面结构

先按旧 `<tr>` 结构定位；找不到时从入口名称逐级向父容器寻找“查看占用”，降低 AutoDL 页面 DOM 小改动导致的持续采集失败。

## 自动开机判定

```text
平台对应入口存在空闲 GPU ID
AND
物理 GPU 可用显存 >= max(8192 MB, 总显存 × 25%)
```

GPU Util 默认只记录，不作为硬门槛。

## 运行数据

默认公共 runtime：

```text
../autodl_watcher_runtime/
```

主要文件：

```text
logs/watcher.log
state.json
usage/occupancy.db
usage/exports/
login_profile/
browser_profile/
```

## PowerShell 直接运行

激活自己的 Python 环境后：

```powershell
python -m pip install -e . --no-deps
python -m autodl_watcher.login
python -m autodl_watcher.main --entry 2
```

只观察：

```powershell
python -m autodl_watcher.main --dry-run
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

v0.5.0 发布包：51 项单元测试通过。

完整版本变化见同级文件 [`CHANGELOG.md`](CHANGELOG.md)。
