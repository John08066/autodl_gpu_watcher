# Agent MCP 项目知识

只保存项目特有、需要重新探索才能获得的结论。历史笔记不等于当前服务状态或操作授权；接口使用前核对当前版本、账号和目标。

## 私有云开关机与身份核对

- 适用：AutoDL私有云 `private.autodl.com`，v0.6.8；核验日期2026-09-26。
- `POST /api/v2/instance/power_on`：`{"instance_uuid":"完整实例ID","start_mode":"non_gpu"}` 为无卡开机；`start_mode="gpu"` 为有卡开机。当前接口复用页面捕获的 Authorization，仅放内存，不写笔记、日志或Git。
- `POST /api/v2/instance/power_off`：无卡转换使用 `{"instance_uuid":"完整实例ID","release":"now"}`。只向已获用户授权、完整账号列表核实UUID与入口名一致的实例发送请求。
- `POST /api/v2/instance/list`：从真实页面请求捕获租户标识，再传 `tenant_uuid/page_index/page_size`。读取全部分页，校验 `result_total`、重复UUID及状态字段，不能用一页列表证明没有其他账号实例。
- 判断依赖 `status/start_mode`：无卡运行是 `running/non_gpu`；`starting`和`shutting_down`不是已完成；关机后仅同 UUID 的 `shutdown` 允许进入有卡启动复核。
- 响应 `code=Success` 只证明请求受理。开机成功要继续确认 `running/gpu` 及占用中出现本实例。电源请求超时可能已执行，禁止无条件重发或换实例。
- 实际验证：2026-09-26获准关闭203-1后，201-1完整 `running/non_gpu → shutdown → running/gpu` 由监控程序完成，实际GPU #2属于开机前达标空闲集合。验收后201关机、203原UUID恢复running/non_gpu。仅Success或starting/gpu不能代替最终状态与实际占用验收。
- 证据：`src/autodl_watcher/collectors/platform.py`、`autostart.py`；本机 `runtime/validation/v0.6.8-201-no-gpu-start.json` 与 `runtime/validation/201-conversion/evidence.json`。真实前端公开资源 `my-instance.b6778f53.js` 核对了电源端点（本机副本位于 `runtime/validation/frontend/`）。完整转换与恢复证据为 `runtime/validation/authorized-conversion/evidence.json`。运行证据不含令牌，仍留在Git忽略目录。

## 平台GPU空位、物理显存、用户额度分别核对

- 适用版本v0.6.8；核验日期2026-09-26；实际API/Telemetry与模拟回归分列如下。

- 平台空位不是物理GPU完全空闲。未保留的GPU仍可能有其他绑定或SSH任务；当前策略按显存容量判断，GPU利用率默认仅记录。不要将“有空位”解释成“没有训练任务”。
- 开机前必须取得目标入口完整唯一的GPU INDEX；平台未占用INDEX与同主机新鲜显存达标INDEX要有交集。仅分开检查“有平台空位”和“有显存余量”会串到不同卡。已验证power_on请求只有UUID/start_mode，未验证可指定INDEX的参数；本次实际分配#2符合预检，不能推断以后平台总分配达标卡，仍存在调度与并发窗口。
- A100约40GiB、V100约32GiB、TITAN RTX约24GiB；配置门槛是 `max(8192 MB, 总显存×25%)`，应按样本容量计算，不硬编码型号。Telemetry的接收时间用于过期判定，不能用拉取时刻替代。
- 同物理主机本账号无有卡实例，不等于全租户还有额度。2026-09-26实际201启动被拒绝：`GpuStockReqNum`，服务端提示额度上限1、已使用1。用户说明该平台无卡运行同样计入数量额度；本次在203无卡运行时201GPU启动被拒绝，获准关闭203后201完整有卡转换成功，实际验证了该额度阻塞与解除流程。
- 201关机时203-1的running/non_gpu仍影响数量额度；不能将“无实际GPU占用”当成“数量额度已释放”。证据：前次 `runtime/validation/201-conversion/evidence.json` 与获准释放额度后的 `runtime/validation/authorized-conversion/evidence.json`。本次授权只适用于本次测试，不授予今后跨实例自动关机权限。
- v0.6.8对该明确额度拒绝设置进程内锁，后续只读监控，不继续电源POST。处理额度后用户显式重新开始监控才建立新协调器。尚未发现可用于关机前额度预检的权威接口，完整切换仍可能因额度或容量竞争失败。
- 模拟验证：`tests/test_autostart.py` 覆盖201四卡转换、显存比例、索引错配及额度拒绝不重发；`tests/test_monitor_loop.py` 覆盖二次采集故障后恢复触发。

## 占用接口与弹窗特殊结构

- 适用版本v0.6.8；实际只读核验日期2026-09-26。

- 实际捕获：`POST /api/v2/gpu_stock/list` 请求体是 `{"machine_id":"主机ID前缀"}`，不是完整实例UUID。响应含 `index/reserved/gpu_bindings/instance_uuid`；官网占用弹窗的“是否占用”对应保留状态，绑定列表可能同时有多个实例。
- 新弹窗六列无独立用户名；旧版七列带用户名。新格式需解析单元格内每个完整实例ID，与全部账号实例列表对照，不能只解析首个ID、只按括号名称识别本人。
- 弹窗异步加载，读取前要等待完整且属于目标入口的快照；校验唯一INDEX、idle/total。202某入口曾有额外空行，仅能在新鲜Telemetry索引完整且额外行完全无占用证据时排除，不要随意容忍行数错误。
- 实际验证：201/A100、202/TITAN RTX、203/V100只读采集；EXE201三轮同时采集平台、Telemetry和占用。证据在 `runtime/validation/exe-readonly-output.txt` 及各验证日志。

## 开机事件不能被请求前的采集故障消耗

- 适用版本v0.6.7故障、v0.6.8修复；模拟回归验证日期2026-09-26。

- v0.6.7曾在evaluator标记已告警后，二次平台或显存采集抛错，主循环只写日志；恢复后GPU一直空闲仍不再触发。
- v0.6.8请求前的采集失败返回 `recheck_failed`，由现有主循环重新武装。发送电源请求后的响应不明使用单独状态，不能与请求前失败统一处理。
- 验证等级：模拟复现旧实现失败，修复后两轮主循环恢复测试通过。位置：`autostart.py` 和 `tests/test_monitor_loop.py`。

## Windows EXE、路径与后台日志

- 适用版本v0.6.8；核验日期2026-09-26；实际只读运行与本地打包验证，201完整有卡转换已实际通过；其他机型验证范围见电源与占用条目。

- v0.6.8公开入口为 `dist/AutoDLWatcher/AutoDLWatcher.exe`（windowed PE）；内部 `_internal/AutoDLWorker.exe`（console PE）用CREATE_NO_WINDOW和stdin/stdout管道启动。windowed bootloader的标准流为None，不能直接当原Python `-m`入口使用。
- GUI按frozen状态选后台命令；外部配置/runtime根目录由公开EXE目录决定，内部worker通过 `AUTODL_APP_ROOT` 使用同一目录，不能写到打包内部目录。
- 打包解释器为 `D:/Dev/Anaconda/envs/autodl-watcher/python.exe`。该Conda环境未激活时，PyInstaller曾选到不匹配OpenSSL DLL导致 `_ssl.pyd` 失败；构建显式收集 `sys.prefix/Library/bin` 的libssl/libcrypto并设置构建PATH。
- EXE包含Python、Tk和Playwright driver，使用系统Edge，不打包浏览器会话或Chromium。无需外部Python PATH，但Windows PowerShell路径仍须可用（登录模块会检查专用Edge进程）。
- 构建先写 `.tools/build/release` 再合并生成文件，避免PyInstaller清理用户已登录的发行目录。已有外部config/runtime/.ui要保留；干净分发另打包白名单文件。
- 实际验证：无Python PATH三轮真实只读监控成功；148项模拟/本地测试通过，覆盖无控制台PE、空格路径、后台管道与重建保留配置；最终两个EXE内18个项目模块递归字节码与源码一致，未启动的构建目录无个人运行数据。证据：`tools/build_exe.py`、`tools/worker_entry.py`、`tests/test_package_layout.py`、`tests/test_gui_window.py`；最终实测 `runtime/validation/exe-final-readonly-output.txt`，完整测试 `runtime/validation/full-tests.txt`。干净包为 `dist/AutoDLWatcher-v0.6.8.zip`，不复制运行后的dist。
