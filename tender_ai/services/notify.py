"""Benachrichtigen, was seit dem letzten Lauf passiert ist (Stufe 8).

Der Ablauf ist in einer Reihenfolge gebaut, die keine Meldung verschluckt:

1. Ereignisse sammeln (``notify.events``)
2. je Kanal abziehen, was dort schon gemeldet wurde
3. zustellen
4. **erst nach erfolgreicher Zustellung** protokollieren

Scheitert Schritt 3, bleibt nichts protokolliert - die Meldung kommt beim
naechsten Lauf erneut. Umgekehrt waere schlimmer: eine Frist, von der niemand
erfaehrt, weil der Mailserver kurz nicht erreichbar war.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..core.errors import ConfigError
from ..core.http import build_http_client
from ..core.logging import get_logger
from ..database.repository import TenderRepository
from ..database.session import session_scope
from ..notify.channels import ChannelError, EmailChannel, NotificationChannel, WebhookChannel
from ..notify.events import KINDS, NotificationEvent, collect_events
from ..notify.render import render_text, summary_line

log = get_logger(__name__)


@dataclass(slots=True)
class ChannelResult:
    channel: str
    ok: bool = True
    sent: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "ok": self.ok, "sent": self.sent, "error": self.error}


@dataclass(slots=True)
class NotificationReport:
    events: list[NotificationEvent] = field(default_factory=list)
    channels: list[ChannelResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.channels)

    @property
    def sent(self) -> int:
        return sum(result.sent for result in self.channels)

    @property
    def summary(self) -> str:
        return summary_line(self.events)

    def as_text(self) -> str:
        return render_text(self.events)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "found": len(self.events),
            "sent": self.sent,
            "summary": self.summary,
            "events": [event.as_dict() for event in self.events],
            "channels": [result.as_dict() for result in self.channels],
        }


def _secret(value: Any) -> str | None:
    return value.get_secret_value() if value is not None else None


def build_channels(
    settings: Settings, http: Any, *, only: Sequence[str] | None = None
) -> list[NotificationChannel]:
    """Die konfigurierten Kanaele aufbauen.

    ``only`` waehlt aus, was in der Konfiguration aktiv ist - ein Name, den es
    nicht gibt, ist ein Konfigurationsfehler und keine stille Auslassung.
    """
    wanted = {name.lower() for name in only} if only else None
    known = {"email", "webhook"}
    if wanted and (unknown := sorted(wanted - known)):
        raise ConfigError(
            f"Unbekannte(r) Kanal/Kanaele: {', '.join(unknown)}. "
            f"Bekannt: {', '.join(sorted(known))}."
        )

    channels: list[NotificationChannel] = []
    config = settings.notifications
    if config.email.enabled and (wanted is None or "email" in wanted):
        channels.append(
            EmailChannel(
                config.email,
                user=_secret(settings.smtp_user),
                password=_secret(settings.smtp_password),
            )
        )
    if config.webhook.enabled and (wanted is None or "webhook" in wanted):
        channels.append(WebhookChannel(config.webhook, http, token=_secret(settings.webhook_token)))
    return channels


async def send_notifications(
    settings: Settings,
    *,
    dry_run: bool = False,
    only_channels: Sequence[str] | None = None,
    limit: int | None = None,
) -> NotificationReport:
    """Meldenswertes sammeln und zustellen.

    ``dry_run`` sammelt und zeigt, stellt aber nichts zu und merkt sich nichts -
    damit laesst sich vor dem ersten scharfen Lauf sehen, was ankaeme.
    """
    config = settings.notifications
    if not config.enabled:
        log.info("notifications_disabled")
        return NotificationReport(dry_run=dry_run)

    kinds = [kind for kind in config.kinds if kind in KINDS]
    if unknown := sorted(set(config.kinds) - set(KINDS)):
        raise ConfigError(
            f"Unbekannte Ereignisart(en) in notifications.kinds: {', '.join(unknown)}. "
            f"Bekannt: {', '.join(KINDS)}."
        )

    max_events = limit if limit is not None else config.max_events
    http = build_http_client(settings.http, settings.cache_dir)
    try:
        channels = build_channels(settings, http, only=only_channels)
        report = NotificationReport(dry_run=dry_run)

        with session_scope(settings.database_url) as session:
            repository = TenderRepository(session, settings.dedup)
            all_events = collect_events(
                repository,
                kinds=kinds,
                deadline_days=config.deadline_days,
                limit=max_events,
            )

            if dry_run:
                report.events = all_events[:max_events]
                log.info("notifications_dry_run", events=len(report.events))
                return report

            report.events = all_events[:max_events]

            if not channels:
                # Kein Kanal aktiv: sammeln und zeigen, aber nichts als
                # gemeldet verbuchen - sonst waere die erste echte Mail leer.
                log.info("no_notification_channel", events=len(all_events))
                return report

            for channel in channels:
                report.channels.append(await _deliver(channel, all_events, repository, max_events))
            session.commit()

        log.info(
            "notifications_done",
            found=len(all_events),
            sent=report.sent,
            channels=[result.channel for result in report.channels],
        )
        return report
    finally:
        await http.aclose()


def _pending(
    channel: NotificationChannel,
    events: list[NotificationEvent],
    repository: TenderRepository,
    limit: int,
) -> list[NotificationEvent]:
    """Was dieser Kanal noch nicht gesehen hat - hoechstens ``limit`` Stueck."""
    known = repository.already_notified(channel.name, (event.dedupe_key for event in events))
    return [event for event in events if event.dedupe_key not in known][:limit]


async def _deliver(
    channel: NotificationChannel,
    events: list[NotificationEvent],
    repository: TenderRepository,
    limit: int,
) -> ChannelResult:
    pending = _pending(channel, events, repository, limit)
    if not pending:
        return ChannelResult(channel=channel.name, sent=0)

    try:
        await channel.deliver(pending)
    except ChannelError as exc:
        log.error("notification_failed", channel=channel.name, error=str(exc))
        return ChannelResult(channel=channel.name, ok=False, error=str(exc))

    # Erst jetzt merken: was nicht ankam, wird beim naechsten Lauf erneut gemeldet.
    repository.record_notifications(
        channel.name,
        ((event.tender_id, event.kind, event.dedupe_key) for event in pending),
    )
    return ChannelResult(channel=channel.name, sent=len(pending))


def send_notifications_sync(settings: Settings, **kwargs: Any) -> NotificationReport:
    """Synchroner Einstieg fuer CLI und Skripte."""
    return asyncio.run(send_notifications(settings, **kwargs))
