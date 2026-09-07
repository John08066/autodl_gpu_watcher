"""
通知模块 — 在 evaluator 生成 AvailabilityAlert 后发送通知。

支持的输出渠道：
    - console: 控制台直接打印（默认启用）
    - email: SMTP 发送邮件（需配置 SMTP_* 环境变量，默认禁用）
"""
from .console import ConsoleNotifier
from .email import EmailNotifier

__all__ = ["ConsoleNotifier", "EmailNotifier"]
