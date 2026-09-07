"""
AutoDL GPU Watcher — 私有云 GPU 资源监控与自动开机工具。

整体架构（自顶向下）：
    run_watcher.py              入口脚本，将 src/ 加入 sys.path
    └─ main.py                  main() 主循环：采集→评估→通知→开机→持久化
        ├─ cli.py               CLI 参数解析与主机名/入口名归一化
        ├─ config.py            YAML 配置加载，所有 dataclass 定义
        ├─ models.py            核心数据模型（PlatformHost, GpuSample, AvailabilityAlert ...）
        ├─ evaluator.py         GPU 显存连续性评估，生成 AvailabilityAlert
        ├─ autostart.py         自动开机协调器：入口排序、二次确认、发送 power_on
        ├─ state_store.py       状态持久化（JSON 原子写入）
        ├─ collectors/
        │   ├─ platform.py      Playwright 浏览器自动化 → AutoDL 平台 GPU ID 空位
        │   └─ telemetry.py     HTTP 请求自建 Telemetry API → 物理 GPU 显存快照
        ├─ notifiers/
        │   ├─ console.py       控制台输出告警
        │   ├─ email.py         SMTP 邮件告警
        │   └─ formatting.py    告警文本格式化
        ├─ usage.py             SQLite 占用日志（记录谁在用 GPU）
        ├─ usage_report.py      从 SQLite 导出 CSV 报表 + 用户 GPU 占用时长统计
        ├─ login.py             独立脚本：打开浏览器完成 AutoDL 登录，保存会话
        ├─ demo.py              手动演示 evaluator 的告警触发逻辑
        └─ autostart_demo.py    手动验证 power_on 开机链路的独立脚本
"""

__version__ = "0.5.1"
