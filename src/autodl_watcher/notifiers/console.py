from __future__ import annotations  # 控制台通知器 — 将开机告警直接打印到控制台。

from ..models import AvailabilityAlert
from .formatting import format_alert


class ConsoleNotifier:  # 将 AvailabilityAlert 格式化后打印到 stdout。

    def send(self, alert: AvailabilityAlert) -> None:  # 将开机达标事件格式化后同步输出到标准终端。
        print(format_alert(alert), flush=True)
