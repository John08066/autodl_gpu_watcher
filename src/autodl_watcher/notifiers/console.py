"""
控制台通知器 — 将开机告警直接打印到控制台。

在 demo.py 和手动测试场景中使用。
在 main.py 的生产循环中，console_notifier 不作为独立组件存在，
告警信息直接通过 print() 在 main.py 主循环中输出。
"""
from __future__ import annotations

from ..models import AvailabilityAlert
from .formatting import format_alert


class ConsoleNotifier:
    """将 AvailabilityAlert 格式化后打印到 stdout。"""

    def send(self, alert: AvailabilityAlert) -> None:
        """功能：
            将开机达标事件格式化后同步输出到标准终端。

        参数：
            alert (AvailabilityAlert)：评估器生成的开机达标事件，包含目标主机、平台空位和达标 GPU。

        返回：
            None：函数通过副作用完成初始化、输出、持久化或资源管理。
        """
        print(format_alert(alert), flush=True)
