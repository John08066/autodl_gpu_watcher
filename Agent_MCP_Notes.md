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

- 适用版本v0.6.8～v0.6.11；实际只读核验日期2026-09-26。

- 实际捕获：`POST /api/v2/gpu_stock/list` 请求体是 `{"machine_id":"主机ID前缀"}`，不是完整实例UUID。响应含 `index/reserved/gpu_bindings/instance_uuid`；官网占用弹窗的“是否占用”对应保留状态，绑定列表可能同时有多个实例。
- 新弹窗六列无独立用户名列，但每条绑定的实例ID后括号包含网页用户名；旧版七列带独立用户名。必须拆分每个完整实例ID，与全部账号实例列表对照；括号名字只能展示，不能代替UUID判本人，否则重名绕过归属核验。v0.6.11用独立display_name保留标签，缺名显示未知；账号查询失败仍显名，但身份证据缺失继续阻断电源。
- v0.6.11核验：实际203-1/203-2六列表格各读到四条带名绑定，证据`runtime/validation/v0.6.11/live-names.json`（无凭据）；多绑定必须在原始GPU行完整性校验之后拆分，否则误判重复INDEX。半角/全角、昵称嵌括号、同名不同UUID、接口失败保名及旧七列由模拟回归覆盖：`tests/test_monitor_loop.py`、`tests/test_usage.py`。最终EXE三轮只读监控输出两份完整占用快照，名字与该弹窗一致，证据`runtime/validation/v0.6.11/exe-readonly.txt`；初次缺席复核会推迟下一次占用采集，不能将每轮监控误当每轮占用快照。本轮电源0操作。
- 弹窗异步加载，读取前要等待完整且属于目标入口的快照；校验唯一INDEX、idle/total。202某入口曾有额外空行，仅能在新鲜Telemetry索引完整且额外行完全无占用证据时排除，不要随意容忍行数错误。
- 实际验证：201/A100、202/TITAN RTX、203/V100只读采集；EXE201三轮同时采集平台、Telemetry和占用。证据在 `runtime/validation/exe-readonly-output.txt` 及各验证日志。

## 开机事件不能被请求前的采集故障消耗

- 适用版本v0.6.7故障、v0.6.8修复；模拟回归验证日期2026-09-26。

- v0.6.7曾在evaluator标记已告警后，二次平台或显存采集抛错，主循环只写日志；恢复后GPU一直空闲仍不再触发。
- v0.6.8请求前的采集失败返回 `recheck_failed`，由现有主循环重新武装。发送电源请求后的响应不明使用单独状态，不能与请求前失败统一处理。
- 验证等级：模拟复现旧实现失败，修复后两轮主循环恢复测试通过。位置：`autostart.py` 和 `tests/test_monitor_loop.py`。

## Windows EXE、路径与后台日志

- 适用版本v0.6.8～v0.6.10；核验日期2026-09-26；实际只读运行与本地打包验证，201完整有卡转换已实际通过；其他机型验证范围见电源与占用条目。

- v0.6.9公开入口改为项目根目录 `AutoDLWatcher.exe`（v0.6.8在 `dist/AutoDLWatcher/AutoDLWatcher.exe`）（windowed PE）；内部 `_internal/AutoDLWorker.exe`（console PE）用CREATE_NO_WINDOW和stdin/stdout管道启动。windowed bootloader的标准流为None，不能直接当原Python `-m`入口使用。
- GUI按frozen状态选后台命令；外部配置/runtime根目录由公开EXE目录决定，内部worker通过 `AUTODL_APP_ROOT` 使用同一目录，不能写到打包内部目录。
- 打包解释器为 `D:/Dev/Anaconda/envs/autodl-watcher/python.exe`。该Conda环境未激活时，PyInstaller曾选到不匹配OpenSSL DLL导致 `_ssl.pyd` 失败；构建显式收集 `sys.prefix/Library/bin` 的libssl/libcrypto并设置构建PATH。
- EXE包含Python、Tk和Playwright driver，使用系统Edge，不打包浏览器会话或Chromium。无需外部Python PATH，但Windows PowerShell路径仍须可用（登录模块会检查专用Edge进程）。
- 构建先写 `.tools/build/release` 再合并生成文件，避免PyInstaller清理用户已登录的发行目录。已有外部config/runtime/.ui要保留；干净分发另打包白名单文件。
- v0.6.8历史验证：无Python PATH三轮真实只读监控成功；148项模拟/本地测试通过，覆盖无控制台PE、空格路径、后台管道与重建保留配置；最终两个EXE内18个项目模块递归字节码与源码一致，未启动的构建目录无个人运行数据。证据：`tools/build_exe.py`、`tools/worker_entry.py`、`tests/test_package_layout.py`、`tests/test_gui_window.py`；最终实测 `runtime/validation/exe-final-readonly-output.txt`，完整测试 `runtime/validation/full-tests.txt`。干净包为 `dist/AutoDLWatcher-v0.6.8.zip`，不复制运行后的dist。

- v0.6.9实际验证：174项测试全部通过，最终两个EXE内19个项目模块与当前源码递归字节码一致；无外部Python PATH两次刷新各5台服务器、三轮201只读监控通过。干净包`dist/AutoDLWatcher-v0.6.9.zip`仅含未启动构建文件、config与说明；证据`runtime/validation/v0.6.9/`下的`full-tests.txt`、`discover-1.txt`、`discover-2.txt`、`monitor.txt`和`build-final.txt`。

## 网页响应体丢失与登录核验

- 适用v0.6.9；核验日期2026-09-26；模拟测试复现原CDP故障并验证恢复，实际EXE两次刷新与三轮201只读采集通过；没有电源操作。
- 浏览器 `Response.json()` 可抛 `Network.getResponseBody: No resource with given identifier found`；原v0.6.8只捕获超时，错误绕过恢复。这是响应读取故障，不是认证失效证据；有已捕获Authorization/请求体时改用新context.request响应，只重试machine/list只读请求，不重发电源POST。
- 登录资料同步不代表会话有效。`WATCHER_SESSION`仅输出checking/valid/invalid/unknown、消息和核验时间；成功主机接口才标valid，登录页/401/403证据才标invalid。未知或网络失败不能猜测为过期。
- GUI管道/消息错误必须仍处理任务结束并继续事件调度；真实后台未结束前不允许另一个任务并发使用同一profile。证据：`collectors/platform.py`、`gui.py`、`login.py`、`main.py`及对应故障回归测试。
- 实际验证：仅复制config到临时目录，以隔离空profile运行最终worker discover；真实跳转/login后输出invalid并退出1，无Traceback/PyInstaller异常，原会话不变。证据`runtime/validation/v0.6.9/no-session.txt`。正常会话核验成功只证明检查时刻有效，不能保证后续持续有效。
- 集成验证：运行源码collector，注入一次与用户堆栈相同的响应体读取错误；真实只读machine/list新响应一次恢复，读取3个物理主机/5入口，电源操作0次。这是模拟故障＋真实只读恢复，不是自然复现；证据`runtime/validation/v0.6.9/response-recovery.json`。


## 跨入口无卡额度释放与个人列表边界

- 适用v0.6.10；核验日期2026-09-26。电源流程仅模拟测试，真实接口仅只读核验归属；本轮没有实际关机/开机。
- 当前普通账号实际验证：POST `/api/v2/user/get`（空对象）返回data中的user_uuid/tenant_uuid；登录响应将is_admin/is_platform_admin存入localStorage的user，两者不在user/get中。普通 `/api/v2/instance/list` 返回个人实例且无owner字段；其他用户的7条GPU绑定UUID均未出现在个人列表。管理员前端另用 `platform_admin/v1/instance/list`，不能凭tenant_uuid认定实例属于本人。
- 批量释放读取完整列表前核对API身份、浏览器身份和捕获tenant一致，并要求两个管理员标志严格false；管理员/缺失标志/身份错配阻断自动释放。仅当前普通账号范围已实际验证，不推断管理员列表的所有权语义。代码`collectors/platform.py:get_account_instances(require_personal_scope=True)`，证据`runtime/validation/v0.6.10/account-scope.json`；拒绝条件由`tests/test_collectors.py`模拟验证。
- 目标空位与新鲜显存同INDEX达标后，关闭个人列表中其它running/non_gpu实例（含未配置入口），记录已发off集合并等待全部shutdown，再关原目标无卡实例或启动原已关机目标。有卡实例不关闭；响应丢失只读查询、不重复off/on。后来出现的新无卡实例必须重新核对目标容量；目标已发off但尚未shutdown时保持只读等待。最终on前再次核实原UUID、入口、shutdown及同主机本人GPU已空闲。代码`autostart.py`、`main.py`；模拟回归`tests/test_autostart.py`、`tests/test_monitor_loop.py`。
- 其它无卡仍starting/shutting_down时，即使尚未发POST也必须保留pending_switch和事件；否则evaluator一次事件被消耗后不会再次释放。模拟复现并修复；starting/gpu只显示等待，running/gpu才报告运行。
- 普通GUI默认真实开机和全账号无卡释放，`--debug`默认只读；启动GUI仅核验会话，仍须手动开始监控。统计开关通过`--no-usage-report`停止历史SQLite写入，不关闭必要占用核验；日志始终显示。代码`gui.py`、`main.py`；Tk及循环模拟测试验证，窗口1000x800保留。
- Playwright的网络错误Call log会打印请求头，可能含Authorization；通用post_api_json将PlaywrightError转为固定安全说明并from None，避免因果堆栈泄露。没有增加电源重试。已核对本机Playwright driver源码，并用虚构令牌模拟传输/读体错误验证无泄露且每次仅发1请求；证据`tests/test_collectors.py:ApiTransportPrivacyTest`。
- 模拟修复：目标原已shutdown、无需关闭其它实例时，仍必须在convert有卡开机POST前锁原目标和已发请求；否则响应丢失后缺席确认可能重新武装并重复on。成功响应后清锁，由主循环等待实际占用；失联只查询状态。证据`tests/test_autostart.py`的初始已关机目标开机失联回归。
- 实际只读：新增require_personal_scope=True路径成功读取3个当前个人实例，identity API和浏览器身份校验均通过，接口白名单禁止电源请求。证据`runtime/validation/v0.6.10/personal-scope-guard.json`。
- 最终验收：204项回归通过；两个EXE各19项目模块与最终源码匹配；无Python PATH读取5入口及三轮201只读采集通过，no-usage-report未创建历史数据库但实时占用核验正常。本轮电源0操作。证据`runtime/validation/v0.6.10/full-tests.txt`、`exe-source-match.json`、`discover.txt`、`monitor-no-reports.txt`；干净分发`dist/AutoDLWatcher-v0.6.10.zip`。
