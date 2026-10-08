# AutoDL 项目关键操作笔记

仅作定位线索，当前代码与服务证据优先；不保存凭据，不授予电源操作或远程发布权限。下文区分实际验证、模拟回归与未验证限制；代码路径相对 `src/autodl_watcher/`，证据路径相对 `runtime/validation/`。

## 电源接口与额度

- 私有云开机：`POST /api/v2/instance/power_on`，`{"instance_uuid":"完整ID","start_mode":"non_gpu"}`；有卡模式为 `gpu`。关机：`POST /api/v2/instance/power_off`，`{"instance_uuid":"完整ID","release":"now"}`。网页 Authorization 仅存内存。2026-09-26实测；代码 `collectors/platform.py`、`autostart.py`。
- 请求前锁定目标；`code=Success`仅代表受理，须确认同UUID的 `running/gpu`及实际占用。响应不明只读查询，不盲目重发或换实例。v0.6.10边界模拟回归：`tests/test_autostart.py`。
- 无卡实例也可能占数量额度；`GpuStockReqNum`出现后，本进程停止电源重试，无已验证额度预检接口。2026-09-26获准关闭203后201转换成功：`authorized-conversion/evidence.json`。
- 批量流程：目标容量达标 → 获取完整个人列表 → 关闭本人其它无卡实例并确认全部shutdown → 转换原目标并复核容量/身份。排除管理员、身份不明、列表不完整及有卡实例；跨入口批量流程仅模拟验证。

## 本人身份及物理GPU

- `POST /api/v2/user/get`提供 user_uuid/tenant_uuid，管理员标志来自 `localStorage.user`。批量电源要求API/浏览器身份及租户一致、两个管理员标志严格false。普通 `instance/list`已验证为个人范围，不推断管理员语义。2026-09-26只读实测：`v0.6.10/account-scope.json`；代码 `get_account_instances(require_personal_scope=True)`。
- 空位、显存、账号额度不可互代。关机和有卡启动前，目标入口空闲INDEX须与新鲜显存达标INDEX相交；Telemetry按接收时间判过期。`power_on`指定INDEX未验证，实际分配可能不同。
- 占用表先校验入口、唯一INDEX、idle/total，再拆多实例绑定；仅在新鲜Telemetry完整且额外行无任何占用证据时排除202额外空行。v0.6.8起201/A100、202/TITAN RTX、203/V100只读实测；回归 `tests/test_collectors.py`。

## 占用弹窗与界面

- `POST /api/v2/gpu_stock/list`：`{"machine_id":"主机ID前缀"}`。六列版用户名在完整实例ID后括号内，七列版独立列；支持半角/全角及昵称嵌括号。名字仅展示，六列本人归属按完整账号实例列表核验。2026-09-26只读实测：`v0.6.11/live-names.json`；代码 `main.py:_instance_bindings_from_cell`。
- 弹窗入口不等于实例所属入口，同一203物理GPU可出现在203-1/203-2。v0.7.0标签固定为用户选择，状态查询所选UUID；2026-09-28模拟及成品EXE三轮只读实测：`v0.7.0/verification.json`，回归 `tests/test_v070.py`。
- v0.7.0补充版统一采样间隔，默认60秒，每轮同时采平台、Telemetry和名单，旧独立名单缓存结论失效。缺席须两次可靠采样，缺入口/身份不明/失败不算。图表用过滤空位前的Telemetry并按账号可见主机筛选，0空位不代表无数据。2026-09-28模拟及成品只读实测：`v0.7.0-dashboard/gui-live-verification.json`；代码 `main.py`、`session.py`，回归 `tests/test_v070.py`、`tests/test_gui.py`。
- v0.8.1“可开机”须当前完整实例列表的入口/UUID匹配已启用配置，不能只查本地targets；SSH配置独立判断。2026-09-30只读实测及模拟回归：`v0.8.1/platform-evidence.json`；代码 `gui.py:discovery_rows`。

## 登录和网络故障

- Telemetry `RemoteDisconnected`不代表登录失效或SSH断连。2026-10-07实测日志15:03—15:09连续6轮失败后15:09:57恢复；本机自有连接快照无积压，无429证据，断连源未确认。v0.8.10模拟回归：每次采集最多2次GET，连续失败60/120/240/300秒退避，429不立即重试并遵守Retry-After；冷却不请求，失败不评估/开机，成功复用健康连接，退出关闭Session。requests默认非stream响应会消费响应体，旧代码未显式close本身不能证明泄漏。当前环境不能证明GUI历史路由，trust_env=False不能绕过TUN。证据 `runtime/validation/telemetry-recovery/诊断报告.md`、351项离线回归；代码 `collectors/telemetry.py`、`main.py`。
- `Network.getResponseBody: No resource with given identifier found`表示响应体丢失；捕获请求上下文后用新API响应恢复，仅有限重试只读查询，不重发电源POST。v0.6.9模拟故障及真实只读恢复：`v0.6.9/response-recovery.json`。
- 登录页、401/403或明确登录超时业务响应才证明失效，HTTP200也可能登录超时；未知错误、超时、429、5xx不能统一判过期。v0.7.0在 `machine/list`和通用API识别业务超时，GUI退出任务并恢复登录入口；旧版漏判有实际日志，修复回归 `tests/test_v070.py`为模拟验证。
- 同步登录资料后须真实接口核验，仅证明检查时刻有效；GUI管道报错也须处理任务退出，旧后台退出前不得并发用同一profile。Playwright Call log可能泄露Authorization，`post_api_json`仅输出固定安全错误；2026-09-26依赖审查及隐私模拟回归 `tests/test_collectors.py`。

## Windows EXE与验证

- 图标验收（2026-10-07，v0.8.9）：窗口/Tk图标、运行中任务栏、Shell快捷方式与EXE资源是不同来源；只修改窗口不能宣称EXE图标变化。ICO原样副本保留全部帧，Windows实际16/32帧通过WM_GETICON及GetIconInfo像素、ShellGetFileInfo和屏幕截图交叉核验；空句柄继续查GCLP_HICONSM/HICON。新进程只读icon.json恢复与热更换均实测通过。定制打包--icon保留3个测试作者帧；默认发行不嵌入个人图标。源码GUI验证mock刷新，未连接平台/SSH；正式用户图片、其它DPI及旧固定入口未实机验收。证据 `runtime/validation/icons-development/`，345项完整回归及8项加强图标检查。
- 主程序在仓库根 `AutoDLWatcher.exe`，worker在 `_internal/AutoDLWorker.exe`，以 `AUTODL_APP_ROOT`共用外部config/runtime。先构建至 `.tools/build/release`再合并，保留会话、配置、偏好和旧发行目录；干净包只取未运行的暂存目录。
- 解释器 `D:/Dev/Anaconda/envs/autodl-watcher/python.exe`；收集Conda `Library/bin`的libssl/libcrypto并设置构建PATH。包含Python/Tk/Playwright driver，使用系统Edge，须无外部Python PATH验收。代码 `tools/build_exe.py`，回归 `tests/test_package_layout.py`。
- 辅助PowerShell用 `SystemRoot/System32/WindowsPowerShell/v1.0/powershell.exe`绝对路径。PATH故障须限制父进程或成品继承环境，仅改 `subprocess.run(env=...)`不足；窗口出现不能代替刷新/登录验收。v0.7.0修复WinError 2，2026-09-28精简PATH全流程只读实测：`v0.7.0-path-repair/gui-live-verification.json`；代码 `login.py`，回归 `tests/test_login.py`。
- 普通GUI默认真实开机，`--debug`默认只读；打开窗口仅核验会话，监控需手动开始。停止用自有stop文件，等worker安全退出再关窗，不强杀共享浏览器；窗口1000x800沿用用户设置。
- 改写Git说明前保存 `git bundle --all`及原refs，逐提交核验代码树、作者时间、映射父关系不变；本地改写不代表远程同步。v0.7.0备份 `.tools/backups/v0.7.0-before/history.bundle`。

## 训练进度与SSH只读采集

- 启动器与训练PID（2026-10-08，v0.8.11）：4090实采确认run_q18.py/PID1271026启动train_q18.py/PID1280972，旧training_roots只保留祖先进程，导致默认解析及AI提示词排除真正训练日志；规则缓存仅有校验失败信息，无原始AI响应，不能断言具体返回结构。现按不同Python入口保留真实子训练、合并同入口DataLoader，并防止历史引用插回启动器；GPU归属仍核验PID/start_ticks。PID不匹配在付费请求前阻止。证据runtime/validation/rules-4090-development/live-before.json、live-after.json（实际只读）；357项回归及Tk按钮到主卡片规则应用（模拟AI，零付费），原失败缓存不伪造为已生成。

- 通用日志与AI边界（2026-10-07，v0.8.8）：全局 `.ui/ai.json`/Windows凭据供全部服务器共用，含Flex、可编辑监控提示词与调用时计价；`.ui/training_rules.json`按连接范围和进程启动身份缓存，自动模式每任务只尝试一次，采样不重复调用AI。仅解释字段路径/受限模板，不能执行模型代码、清除错误或参与开机。确认v0.8.7误判根因：Q10同条日志外层时间比JSON time晚约0.76ms，严格时间比较拒绝有效规则；现按原记录位置应用、未覆盖行默认解析，生成窗口读取主卡片真实状态。`.ui/ai_usage.sqlite3`独立记录返回用量/单价快照，包括规则无效请求；缺失为未知、历史不补造。验证：336项完整回归及收尾13项规则回归；203已有真实样本/用户缓存规则本地回放17700→18100，API仅模拟、本轮无远端连接。证据 `runtime/validation/ai-usage-development/`。全局按钮仍放标签栏右侧，独立工具栏会压缩底部六行日志高度。


- v0.8.0使用已有SSH别名、`BatchMode=yes`、`ClearAllForwardings=yes`及严格主机校验；stdin执行标准库探针，不写远端文件。本人UID训练根进程以boot_id/PID/start_ticks识别，不重复统计DataLoader子进程，日志关联打开的fd；仅Python进程读cwd，无关进程PermissionError不代表列表残缺。2026-09-30在203-1/4090只读实测：`v0.8.0/gui-live-verification.json`；代码 `remote_probe.py`。
- v0.8.3识别 `train_*.py`及 `TRAIN {...}`/`EVALUATED 分支 {...}`；批次JSON不替代训练事件，旧默认列表仅内存迁移，不放宽自定义范围。P03-T01的 `steps_per_arm`累计、Epoch从0开始、`EPOCH_END`不代表完成；日志PID匹配后，`peak_gpu_allocated`仅代表本人PyTorch日志峰值，不是NVML当前用量。2026-10-03核验远端 `/root/DeepfakeBench-prd-common/scripts/train_p03_t01.py`：`v0.8.3/live-snapshot.json`；代码 `training.py`、`remote_probe.py`。 v0.8.5补充：Q10的`steps/steps_total`为当前/总步数，Epoch从1开始；`EPOCH_DONE arm epoch steps ... val {...}`为该分支验证指标，须沿用相邻事件PID归属，不能按P03加1。2026-10-07实际203核验：PID259659在tmux运行，旧PID257824已退出且共用追加日志，并非当前训练漏识别；原日志窗口曾混入joint_batch_trace.jsonl。证据`v0.8.5/live-before.jsonl`、`live-after.json`；非train入口发现为模拟验证，仍按本人/项目范围读取。
- DeepfakeBench `training/train.py`为 `range(start_epoch,nEpochs+1)`，0..30共31轮，`Epoch[1]`为第2轮；`Test Done!`不代表整体完成，各测试集保留自己的epoch/step。DFDC曾49分钟无新日志，默认120分钟仅提示待核查。2026-09-30日志/源码实测；代码 `training.py`，OOM/未知退出/重复打印模拟回归 `tests/test_training.py`。
- 203容器NVML宿主PID无法直接匹配容器/proc，NSpid/fdinfo及平台接口未提供已验证进程显存来源；显示“GPU关联/本人显存无法核验”。裸机须本人PID、启动时间、GPU UUID相符后统计。整卡利用率/显存及系统mem_usage不代表任务用量；旧日志无统计不适用于新P03日志峰值。2026-09-30至10-03只读实测：`v0.8.1/platform-evidence.json`、`v0.8.1/*-current.json`、`v0.8.2/container-memory-evidence.json`；代码 `remote_probe.py`。他人仅读GPU进程元信息，不读日志。

## SSH接入故障定位

- 2026-10-03在203只读实测 `Exceeded MaxStartups`：配置 `10:30:100`、未认证连接11~12个、失败登录约250~300次/分钟且轮换用户名，符合口令扫描；训练继续、OOM=0。仅见127.0.0.1，不能据此封禁，须平台转发前核查来源。证据解释新连接拒绝，不解释已认证VS Code最初25秒失联；不是探针解析故障，保活不能修复准入拒绝。证据 `ssh-203-20261003/诊断报告.md`、`ssh-203-20261003/login-timeline-*.json`。
- 同日获准在203-1新增 `/etc/ssh/sshd_config.d/00-autodl-watcher-key-only.conf`：仅公钥认证，禁用密码/交互认证，`PermitRootLogin prohibit-password`、`LoginGraceTime 20`、`MaxAuthTries 3`，MaxStartups不变。核验主机/hash/公钥，设180秒回退，`sshd -t/-T -C`后SIGHUP重载，两个新连接及原会话/训练标识复核成功。备份 `/root/.autodl-watcher-backups/ssh-20261003-v084/sshd_config.before`；恢复仅移除专用文件，校验后重载当前已核验监听PID，勿用历史PID。重建/平台覆盖后重验，未部署至4090或新服务器。实际验证：`v0.8.4/ssh-hardening.json`。
- v0.8.4复用一个已认证OpenSSH进程按行采样，连接失败仅主调度按60/120/240/300秒退避，不叠加线程重拨；停止仅关本对象SSH，不杀共享SSH/VS Code。MaxStartups横幅须 `-v`进入stderr，仅限量内存诊断；60秒采样/15秒保活均非重新登录。2026-10-03在203/4090实测1连接3采样及20秒未认证释放：`v0.8.4/`；回归 `tests/test_v084.py`。
