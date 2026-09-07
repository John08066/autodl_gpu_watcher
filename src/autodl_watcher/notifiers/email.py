from __future__ import annotations  # 邮件通知器 — 通过 SMTP_SSL 发送开机告警邮件。

import os
import smtplib
import ssl
from email.message import EmailMessage

from ..models import AvailabilityAlert
from .formatting import format_alert


class EmailNotifier:  # 通过 SMTP_SSL 发送告警邮件。

    def __init__(self, subject_prefix: str) -> None:  # 初始化邮件通知器并保存邮件主题前缀。
        self.subject_prefix = subject_prefix

    def send(self, alert: AvailabilityAlert) -> None:  # 发送一封开机达标通知邮件。
        required = [  # 检查必需的环境变量
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "SMTP_FROM",
            "SMTP_TO",
        ]
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"Missing SMTP environment variables: {', '.join(missing)}")

        message = EmailMessage()  # 构建邮件
        message["Subject"] = f"{self.subject_prefix} {alert.host} 开机达标 {alert.actionable_count} 张"
        message["From"] = os.environ["SMTP_FROM"]
        message["To"] = os.environ["SMTP_TO"]
        message.set_content(format_alert(alert))

        context = ssl.create_default_context()  # 发送（SSL 连接）
        with smtplib.SMTP_SSL(
            os.environ["SMTP_HOST"],
            int(os.environ["SMTP_PORT"]),
            context=context,
            timeout=20,
        ) as smtp:
            smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
            smtp.send_message(message)
