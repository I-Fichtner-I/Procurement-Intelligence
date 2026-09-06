"""Wege, auf denen eine Meldung ankommt: Mail und Webhook.

Beide Kanaele sind bewusst schmal. Ein Kanal, der scheitert, meldet das als
``ChannelError`` zurueck - der Lauf gilt dann als nicht zugestellt, und nichts
wird als "gemeldet" verbucht. Lieber eine Meldung zweimal als gar nicht.
"""

from __future__ import annotations

import asyncio
import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Any

from ..config import EmailChannelConfig, WebhookChannelConfig
from ..core.errors import TenderAIError
from ..core.http import HttpClient
from ..core.logging import get_logger
from .events import NotificationEvent
from .render import render_payload, render_text, subject_for

log = get_logger(__name__)


class ChannelError(TenderAIError):
    """Zustellung ueber einen Kanal ist fehlgeschlagen."""


class NotificationChannel(ABC):
    """Ein Zustellweg. Der Name landet im Protokoll und steuert die Dedup-Sperre."""

    name: str

    @abstractmethod
    async def deliver(self, events: list[NotificationEvent]) -> None: ...


class EmailChannel(NotificationChannel):
    name = "email"

    def __init__(
        self,
        config: EmailChannelConfig,
        *,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self.config = config
        self._user = user
        self._password = password

    def _message(self, events: list[NotificationEvent]) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject_for(self.config.subject, events)
        message["From"] = self.config.sender
        message["To"] = ", ".join(self.config.recipients)
        message.set_content(render_text(events))
        return message

    def _send(self, message: EmailMessage) -> None:
        """Blockierender SMTP-Versand - laeuft ueber ``deliver`` im Thread."""
        with smtplib.SMTP(self.config.host, self.config.port, timeout=30) as smtp:
            if self.config.starttls:
                smtp.starttls()
            if self._user and self._password:
                smtp.login(self._user, self._password)
            smtp.send_message(message)

    async def deliver(self, events: list[NotificationEvent]) -> None:
        if not self.config.recipients:
            raise ChannelError(
                "Kein Empfaenger konfiguriert (notifications.email.recipients ist leer)."
            )
        message = self._message(events)
        try:
            await asyncio.to_thread(self._send, message)
        except (OSError, smtplib.SMTPException) as exc:
            raise ChannelError(f"Mailversand fehlgeschlagen: {exc}") from exc
        log.info("notification_sent", channel=self.name, events=len(events))


class WebhookChannel(NotificationChannel):
    name = "webhook"

    def __init__(
        self,
        config: WebhookChannelConfig,
        http: HttpClient,
        *,
        token: str | None = None,
    ) -> None:
        self.config = config
        self.http = http
        self._token = token

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json", **self.config.headers}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def deliver(self, events: list[NotificationEvent]) -> None:
        if not self.config.url:
            raise ChannelError("Keine URL konfiguriert (notifications.webhook.url ist leer).")
        payload: dict[str, Any] = render_payload(events)
        try:
            await self.http.request(
                "POST",
                self.config.url,
                json=payload,
                headers=self._headers(),
                # Ein Webhook ist keine Abfrage: weder cachen noch robots.txt
                # befragen - der Endpunkt gehoert dem Betreiber selbst.
                use_cache=False,
                check_robots=False,
            )
        except Exception as exc:  # noqa: BLE001 - jeder Transportfehler ist derselbe Fall
            raise ChannelError(f"Webhook fehlgeschlagen: {exc}") from exc
        log.info("notification_sent", channel=self.name, events=len(events))
