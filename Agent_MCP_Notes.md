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

- 图标验收（2026-10-07，v0.8.9）：窗口/Tk图标、运行中任务栏、Shell快捷方式与EXE资源是不同来源；只修改窗口不能宣称EXE图标变化。ICO原样副本保留全部帧，Windows实际16/32帧通过WM_GETICON及GetIconInfo像素、ShellGetFileInfo和屏幕截图交叉核验；空句柄继续查GCLP_HICONSM/HICON。新进程只读icon.json恢复与热更换均实测通过。定制打包--icon保留3个测试作者帧；默认发行不嵌入个人图标。源码GUI验证mock刷新，未连接平台/SSH；正式用户图片、其它DPI及旧固定入口未实机验收。证据 `runtime/validation/icons-development/`，345项完整回归及8项加强图标检查。
- 主程序固定在仓库根`AutoDLWatcher.exe`，worker在`_internal/AutoDLWorker.exe`，通过`AUTODL_APP_ROOT`共用外部config/runtime。构建先写`.tools/build/release`后合并，保留会话、配置、偏好和旧发行目录。成品干净包只从未运行的暂存目录取文件。
- 打包解释器`D:/Dev/Anaconda/envs/autodl-watcher/python.exe`；显式收集Conda `Library/bin`的libssl/libcrypto并设置构建PATH，避免混用DLL。包含Python、Tk和Playwright driver，浏览器使用系统Edge；无外部Python PATH验收。代码`tools/build_exe.py`、`tests/test_package_layout.py`。
- Windows辅助PowerShell必须使用`SystemRoot/System32/WindowsPowerShell/v1.0/powershell.exe`绝对路径。仅给`subprocess.run(env=...)`删PATH不足以复现Windows程序查找故障，须限制父进程PATH或让成品EXE实际继承该环境；窗口出现也不能代替刷新/登录验收。v0.7.0首版曾因交付窗口继承精简PATH触发WinError 2，同版已修复。2026-09-28实际验证精简PATH下刷新、原生登录与同步核验、只读监控及停止；`login.py`、`tests/test_login.py`、`runtime/validation/v0.7.0-path-repair/gui-live-verification.json`。
- 普通GUI默认真实开机，`--debug`默认只读；打开窗口只核验会话，仍需点击开始监控。停止使用自有stop文件并等worker安全退出，再正常关窗，不强杀共享浏览器。显示高度1000x800沿用用户设置。
- 改写Git说明前保存`git bundle --all`及原refs，逐提交验证代码树、作者时间、映射后的父关系不变；本地改写不代表远程已同步。v0.7.0备份`.tools/backups/v0.7.0-before/history.bundle`。

- v0.8.1“可开机”是当前完整实例列表中入口/UUID与已启用配置匹配，不能仅检查本地targets；2026-09-30实际列表只有201-1、203-1、203-2，202-2旧UUID已无实例。训练SSH是否配置独立判断。代码`gui.py:discovery_rows`，证据`runtime/validation/v0.8.1/platform-evidence.json`；实际只读及模拟回归。

## 训练进度与SSH只读采集

- 通用日志与AI边界（2026-10-07，v0.8.8）：全局 `.ui/ai.json`/Windows凭据供全部服务器共用，含Flex、可编辑监控提示词与调用时计价；`.ui/training_rules.json`按连接范围和进程启动身份缓存，自动模式每任务只尝试一次，采样不重复调用AI。仅解释字段路径/受限模板，不能执行模型代码、清除错误或参与开机。确认v0.8.7误判根因：Q10同条日志外层时间比JSON time晚约0.76ms，严格时间比较拒绝有效规则；现按原记录位置应用、未覆盖行默认解析，生成窗口读取主卡片真实状态。`.ui/ai_usage.sqlite3`独立记录返回用量/单价快照，包括规则无效请求；缺失为未知、历史不补造。验证：336项完整回归及收尾13项规则回归；203已有真实样本/用户缓存规则本地回放17700→18100，API仅模拟、本轮无远端连接。证据 `runtime/validation/ai-usage-development/`。全局按钮仍放标签栏右侧，独立工具栏会压缩底部六行日志高度。

- P03-T01使用`scripts/train_p03_t01.py`；v0.8.2的固定脚本列表及旧日志协议曾漏报。v0.8.3按`train_*.py`识别同类入口、适配`TRAIN {...}`和`EVALUATED 分支 {...}`，批次追踪JSON不能替代训练事件；旧默认列表仅在内存迁移，自定义范围不放宽。`steps_per_arm`累计计数，Epoch从0开始，`EPOCH_END`不等于训练完成。脚本第203行的`peak_gpu_allocated=torch.cuda.max_memory_allocated()`仅作本人日志峰值，需日志PID匹配，不能当作NVML当前用量。2026-10-03实际只读确认，依据远端`/root/DeepfakeBench-prd-common/scripts/train_p03_t01.py`和本地`runtime/validation/v0.8.3/live-snapshot.json`；代码`training.py`、`remote_probe.py`。
- v0.8.0用已有SSH别名，`BatchMode=yes`、`ClearAllForwardings=yes`、严格主机校验；通过stdin执行标准库探针，不落远端文件。本人UID的训练根进程按boot_id/PID/start_ticks识别，DataLoader子进程不重复统计，日志从打开的fd关联。读取无关SSH/systemd进程cwd可能PermissionError，不能因此认定训练列表残缺；仅Python进程读取cwd。代码`remote_probe.py`；2026-09-30实际只读验证203-1及4090，证据`runtime/validation/v0.8.0/gui-live-verification.json`。
- DeepfakeBench当前`training/train.py`使用`range(start_epoch,nEpochs+1)`：0..30共31轮；`Epoch[1]`是显示第2轮。`Test Done!`不等于整体完成；各测试集更新时刻不同，保留各自epoch/step，不能统一标成当前轮次。DFDC测试曾实际观察约49分钟无新日志，因此默认120分钟仅提示待核查。代码`training.py`；2026-09-30实际日志/源码核验，OOM、未知退出及重复打印由`tests/test_training.py`模拟验证。
- 203容器的GPU宿主PID可能在容器/proc中不存在；整卡利用率不能证明本人进程占用。v0.8.1容器仍显示GPU关联/本人显存无法核验，裸机按本人PID、启动时间、GPU UUID直接相符后统计进程显存。当前203的NVML仅返回宿主PID；v0.8.2再次核对NSpid仅有容器PID，GPU fdinfo只有常规文件信息，当时的旧训练日志无显存统计（`runtime/validation/v0.8.2/container-memory-evidence.json`）；已查instance/list、Telemetry及官网监控按钮，未找到进程专属显存来源，不能把系统mem_usage或整卡显存当作任务显存；2026-10-03新P03脚本已提供日志峰值，见上一条，旧任务“无日志统计”结论不适用于它。代码`remote_probe.py`；2026-09-30实际只读，证据`runtime/validation/v0.8.1/platform-evidence.json`与`*-current.json`。其他用户只读取GPU进程元信息，不读取其日志。

## SSH接入故障定位

- 2026-10-03实际只读确认203出现`Exceeded MaxStartups`：有效配置`10:30:100`，未认证连接11—12个，btmp从19:17起激增到约250—300次/分钟、多用户名轮换，符合自动化口令扫描。容器对端与失败日志仅见127.0.0.1，不能按此地址封禁；需在平台转发前核查原始来源。该证据解释新连接/重连拒绝，不单独解释已认证VS Code连接最初的25秒失联。内存约8.4/40 GiB且OOM=0，训练继续；不得把握手前拒绝当作探针或日志解析BUG。证据`runtime/validation/ssh-203-20261003/诊断报告.md`与`login-timeline-*.json`。当时未修改SSH认证、代理或训练；客户端保活不能修复服务端准入拒绝。

- 2026-10-03经授权在203-1新增`/etc/ssh/sshd_config.d/00-autodl-watcher-key-only.conf`：AuthenticationMethods publickey、PasswordAuthentication/KbdInteractiveAuthentication no、PermitRootLogin prohibit-password、LoginGraceTime 20、MaxAuthTries 3，MaxStartups保持10:30:100。先核验主机/配置hash与公钥，启动独立180秒回退守护，`sshd -t/-T -C`验证后向监听PID813发SIGHUP；两个新公钥连接和原会话/训练启动标识复核成功后提交。备份`/root/.autodl-watcher-backups/ssh-20261003-v084/sshd_config.before`，恢复只移除该专用文件、先校验再向当前已核验监听PID重载（不能盲用历史PID）；服务器重建/平台覆盖后须重新核验。该防护未自动部署到4090或新增服务器；证据`runtime/validation/v0.8.4/ssh-hardening.json`，实际验证。

- v0.8.4的训练采样在一个已认证OpenSSH进程内按行发送只读请求，连接故障只由主调度60/120/240/300秒退避；不得叠加线程内重拨。停止只关闭本对象的SSH，不能杀共享SSH/VS Code。MaxStartups横幅需要`-v`才进入stderr，仅读取限量内存诊断；每60秒采集不等于重新认证，15秒保活也不是登录。203/4090真实三轮均1连接3采样，20秒未认证释放实测通过；2026-10-03，证据`runtime/validation/v0.8.4/`及`tests/test_v084.py`。
