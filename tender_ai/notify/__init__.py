"""Benachrichtigungen: was gemeldet wird (``events``), wie es aussieht
(``render``) und worueber es geht (``channels``).

Der Ablauf selbst steht im Dienst ``tender_ai.services.notify``.
"""

from .channels import ChannelError, EmailChannel, NotificationChannel, WebhookChannel
from .events import KINDS, NotificationEvent, collect_events, threshold_for
from .render import render_payload, render_text, summary_line

__all__ = [
    "KINDS",
    "ChannelError",
    "EmailChannel",
    "NotificationChannel",
    "NotificationEvent",
    "WebhookChannel",
    "collect_events",
    "render_payload",
    "render_text",
    "summary_line",
    "threshold_for",
]
