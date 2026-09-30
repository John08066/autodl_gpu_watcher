# AutoDL 项目关键操作笔记

笔记只作定位线索；先核验当前代码和实际状态，不保存凭据，不授予电源操作或远程发布权限。

## 电源接口与额度

- 私有云 `POST /api/v2/instance/power_on`：`{"instance_uuid":"完整ID","start_mode":"non_gpu"}`为无卡，`gpu`为有卡；`POST /api/v2/instance/power_off`：`{"instance_uuid":"完整ID","release":"now"}`。复用网页捕获的Authorization，仅存内存。代码`collectors/platform.py`、`autostart.py`；2026-09-26实际验证。
- `code=Success`仅代表受理，必须再确认同UUID的`running/gpu`及实际GPU占用。请求发出前锁定目标；响应不明只读查询，禁止盲目重发或切换实例。v0.6.10起模拟回归覆盖原目标已关机、响应丢失、关机中停止等边界；`tests/test_autostart.py`。
- 无卡运行也可能占用数量额度；`GpuStockReqNum`表示额度被拒绝，进程内停止电源重试。2026-09-26实际验证：203无卡占用额度时201有卡启动失败，获准关闭203后201完整转换成功。证据`runtime/validation/authorized-conversion/evidence.json`。当前无已验证额度预检接口。
- 目标容量达标后，查询完整个人列表并关闭本账号其它无卡实例，确认全部shutdown后转换原目标，再次核验容量及身份。管理员、身份不明、列表不完整、有卡实例均不能纳入批量关闭。跨入口批量流程仅模拟验证，不能把历史单实例实操结果等同于全部流程已实测。

## 本人身份及物理GPU

- `POST /api/v2/user/get`返回user_uuid/tenant_uuid；is_admin、is_platform_admin来自浏览器localStorage.user。批量电源前要求API身份、浏览器身份、租户一致且两个管理员标志严格false。普通instance/list当前实际验证为个人范围，管理员语义不推断。代码`get_account_instances(require_personal_scope=True)`；2026-09-26实际只读，证据`runtime/validation/v0.6.10/account-scope.json`。
- 平台GPU空位、物理显存与账号数量额度不能互相替代。关机前和有卡启动前，目标入口空闲INDEX必须与新鲜显存达标INDEX相交；接收时间决定Telemetry是否过期。后台power_on未验证可指定INDEX，实际分配仍可能与预检不同。
- 原始占用表每GPU一行，先校验入口、唯一INDEX、idle/total再拆多实例绑定。仅当新鲜Telemetry完整且额外行完全无占用证据时，才可排除202入口偶见的额外空行。v0.6.8起实际只读覆盖201/A100、202/TITAN RTX、203/V100；`tests/test_collectors.py`。

## 占用弹窗与界面

- `POST /api/v2/gpu_stock/list`请求体为`{"machine_id":"主机ID前缀"}`；绑定可能包含多个实例。新版六列把用户名放在每条完整实例ID后的括号内，旧版七列有独立用户名列。名字仅展示，六列本人归属按完整账号实例列表核验；半角/全角及昵称嵌括号均需支持。代码`main.py:_instance_bindings_from_cell`；2026-09-26实际只读，`runtime/validation/v0.6.11/live-names.json`。
- 同一203物理GPU绑定会出现在203-1、203-2两处弹窗，弹窗入口不是实例所属入口。v0.7.0固定监控标签来自用户选择，运行状态来自所选UUID的实例查询；重复入口与重名回归在`tests/test_v070.py`。2026-09-28模拟验证，并以成品EXE实际只读监控203-1三轮核验；证据`runtime/validation/v0.7.0/verification.json`。
- v0.7.0补充版按用户要求改为唯一采样间隔，默认60秒；旧独立占用计时器已删除，每轮平台、Telemetry与占用名单一起采集。之前“名单按独立间隔缓存”的结论失效。连续缺席仍需两次可靠采样；缺入口、身份不明或失败不能证明缺席。图表必须用过滤空位前的Telemetry，并按账号可见主机筛选，平台0空位不代表无GPU数据。代码`main.py`、`session.py`及`tests/test_v070.py`、`tests/test_gui.py`；2026-09-28模拟与成品只读验证，证据`runtime/validation/v0.7.0-dashboard/gui-live-verification.json`。

## 登录和网络故障

- `Network.getResponseBody: No resource with given identifier found`是浏览器响应体丢失；捕获请求上下文后用新API响应恢复，只有限重试只读查询，不重发电源POST。v0.6.9模拟故障配合真实只读恢复验证；`runtime/validation/v0.6.9/response-recovery.json`。
- 登录页、HTTP401/403或业务响应明确登录超时才是失效证据。HTTP200也可能返回“登陆超时，请重新登录”；旧版漏判曾每30秒重试298次，2026-09-27至28实际日志已确认。v0.7.0在machine/list和通用API响应识别该业务错误，GUI模式退出并恢复登录入口；`tests/test_v070.py`模拟验证。未知业务错误、超时、429、5xx不统一当登录过期。
- 登录资料同步后还要用真实接口核验；会话有效只针对检查时刻。GUI管道错误仍须处理任务退出，不得在旧后台退出前并发使用同一profile。
- Playwright传输错误的Call log可能含Authorization；通用API捕获后仅输出固定安全错误，不原样暴露堆栈请求头。`post_api_json`及`tests/test_collectors.py`隐私回归，2026-09-26本地依赖审查和模拟验证。

## Windows EXE与验证

- 主程序固定在仓库根`AutoDLWatcher.exe`，worker在`_internal/AutoDLWorker.exe`，通过`AUTODL_APP_ROOT`共用外部config/runtime。构建先写`.tools/build/release`后合并，保留会话、配置、偏好和旧发行目录。成品干净包只从未运行的暂存目录取文件。
- 打包解释器`D:/Dev/Anaconda/envs/autodl-watcher/python.exe`；显式收集Conda `Library/bin`的libssl/libcrypto并设置构建PATH，避免混用DLL。包含Python、Tk和Playwright driver，浏览器使用系统Edge；无外部Python PATH验收。代码`tools/build_exe.py`、`tests/test_package_layout.py`。
- Windows辅助PowerShell必须使用`SystemRoot/System32/WindowsPowerShell/v1.0/powershell.exe`绝对路径。仅给`subprocess.run(env=...)`删PATH不足以复现Windows程序查找故障，须限制父进程PATH或让成品EXE实际继承该环境；窗口出现也不能代替刷新/登录验收。v0.7.0首版曾因交付窗口继承精简PATH触发WinError 2，同版已修复。2026-09-28实际验证精简PATH下刷新、原生登录与同步核验、只读监控及停止；`login.py`、`tests/test_login.py`、`runtime/validation/v0.7.0-path-repair/gui-live-verification.json`。
- 普通GUI默认真实开机，`--debug`默认只读；打开窗口只核验会话，仍需点击开始监控。停止使用自有stop文件并等worker安全退出，再正常关窗，不强杀共享浏览器。显示高度1000x800沿用用户设置。
- 改写Git说明前保存`git bundle --all`及原refs，逐提交验证代码树、作者时间、映射后的父关系不变；本地改写不代表远程已同步。v0.7.0备份`.tools/backups/v0.7.0-before/history.bundle`。

## 训练进度与SSH只读采集

- v0.8.0用已有SSH别名，`BatchMode=yes`、`ClearAllForwardings=yes`、严格主机校验；通过stdin执行标准库探针，不落远端文件。本人UID的训练根进程按boot_id/PID/start_ticks识别，DataLoader子进程不重复统计，日志从打开的fd关联。读取无关SSH/systemd进程cwd可能PermissionError，不能因此认定训练列表残缺；仅Python进程读取cwd。代码`remote_probe.py`；2026-09-30实际只读验证203-1及4090，证据`runtime/validation/v0.8.0/gui-live-verification.json`。
- DeepfakeBench当前`training/train.py`使用`range(start_epoch,nEpochs+1)`：0..30共31轮；`Epoch[1]`是显示第2轮。`Test Done!`不等于整体完成；各测试集更新时刻不同，保留各自epoch/step，不能统一标成当前轮次。DFDC测试曾实际观察约49分钟无新日志，因此默认120分钟仅提示待核查。代码`training.py`；2026-09-30实际日志/源码核验，OOM、未知退出及重复打印由`tests/test_training.py`模拟验证。
- 203容器的GPU宿主PID可能在容器/proc中不存在；整卡利用率不能证明本人进程占用。v0.8.0容器统一显示GPU关联未确认，裸机仅在本人PID与GPU UUID直接相符时展示索引。当前203一项训练、4090两项训练实际只读验证；不把该验证扩展为所有容器或所有训练框架的保证。
