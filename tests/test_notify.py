"""Stufe 8: Melden, was neu, geaendert oder fristnah ist.

Der Kern dieser Stufe ist nicht das Versenden, sondern die Zusage: **jede
Meldung genau einmal - aber keine verloren.** Danach sind die Tests gebaut.
"""

from __future__ import annotations

import smtplib
from datetime import UTC, datetime, timedelta
from typing import Self

import httpx
import pytest
import respx

from tender_ai.config import Settings
from tender_ai.core.errors import ConfigError
from tender_ai.database.models import NotificationRecord
from tender_ai.database.repository import TenderRepository
from tender_ai.database.session import session_scope
from tender_ai.models.tender import Tender, TenderStatus
from tender_ai.notify.events import collect_events, threshold_for
from tender_ai.notify.render import render_payload, render_text, summary_line
from tender_ai.services import send_notifications

WEBHOOK_URL = "https://hook.test.invalid/tender"

#: Feste Fristen: ein bei jedem Aufruf neu berechneter Zeitstempel saehe fuer
#: die Aenderungserkennung wie eine verschobene Frist aus.
DEADLINE_FAR = datetime.now(UTC) + timedelta(days=30)
DEADLINE_NEAR = datetime.now(UTC) + timedelta(days=2)


def tender(**overrides) -> Tender:
    base = dict(
        id="ted:1",
        source="ted",
        source_id="1",
        title="Lieferung von 2.000 Monitoren",
        contracting_authority="Musterstadt",
        country="DEU",
        submission_deadline=DEADLINE_FAR,
        estimated_value=420000.0,
        currency="EUR",
        status=TenderStatus.OPEN,
    )
    base.update(overrides)
    return Tender(**base)


@pytest.fixture
def stored(settings: Settings):
    """Zwei Ausschreibungen im Bestand: eine fristnah, eine entspannt."""
    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, settings.dedup)
        repository.upsert(tender())
        repository.upsert(
            tender(
                id="ted:2",
                source_id="2",
                title="Wartung von Aufzugsanlagen",
                submission_deadline=DEADLINE_NEAR,
            )
        )
        session.commit()
    return settings


def _enable_webhook(settings: Settings) -> Settings:
    settings.notifications.webhook.enabled = True
    settings.notifications.webhook.url = WEBHOOK_URL
    return settings


# --- Schwellen ----------------------------------------------------------------
def test_deadline_reminder_uses_the_tightest_reached_threshold():
    """Je Schwelle einmal - und bei 2 Tagen ist die 3-Tage-Schwelle die richtige."""
    assert threshold_for(9, [7, 3, 1]) is None
    assert threshold_for(7, [7, 3, 1]) == 7
    assert threshold_for(4, [7, 3, 1]) == 7
    assert threshold_for(2, [7, 3, 1]) == 3
    assert threshold_for(0, [7, 3, 1]) == 1


# --- Sammeln ------------------------------------------------------------------
def test_collects_new_and_deadline_events(stored: Settings):
    with session_scope(stored.database_url) as session:
        events = collect_events(
            TenderRepository(session, stored.dedup),
            kinds=["new", "deadline"],
            deadline_days=[7, 3, 1],
        )

    by_kind = {(event.kind, event.tender_id) for event in events}
    assert ("new", "ted:1") in by_kind
    assert ("new", "ted:2") in by_kind
    # Nur die fristnahe Ausschreibung loest eine Fristmeldung aus.
    assert ("deadline", "ted:2") in by_kind
    assert ("deadline", "ted:1") not in by_kind
    # Dringendstes zuerst.
    assert events[0].tender_id == "ted:2"


def test_collects_changes_of_watched_fields(stored: Settings):
    with session_scope(stored.database_url) as session:
        repository = TenderRepository(session, stored.dedup)
        repository.upsert(tender(title="Lieferung von 3.000 Monitoren"))
        session.commit()

        events = collect_events(repository, kinds=["changed"], deadline_days=[7])

    assert [event.kind for event in events] == ["changed"]
    assert "title" in events[0].detail or events[0].extra.get("field") == "title"


def test_expired_tenders_are_not_reported(settings: Settings):
    """Eine abgelaufene Frist ist keine Meldung mehr wert."""
    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, settings.dedup)
        repository.upsert(tender(submission_deadline=datetime.now(UTC) - timedelta(days=1)))
        session.commit()

        events = collect_events(
            repository, kinds=["new", "deadline", "changed"], deadline_days=[7, 3, 1]
        )
    assert events == []


# --- Zustellung und Dedup -----------------------------------------------------
@respx.mock
async def test_webhook_receives_events_once(stored: Settings):
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    settings = _enable_webhook(stored)

    first = await send_notifications(settings)
    assert first.ok
    assert first.sent >= 2
    assert route.call_count == 1

    payload = route.calls[0].request.content.decode()
    assert "Monitoren" in payload

    # Zweiter Lauf: nichts hat sich geaendert, also geht auch nichts raus.
    second = await send_notifications(settings)
    assert second.ok
    assert second.sent == 0
    assert route.call_count == 1


@respx.mock
async def test_new_event_after_a_change_is_delivered(stored: Settings):
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
    settings = _enable_webhook(stored)
    await send_notifications(settings)

    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, settings.dedup)
        repository.upsert(tender(estimated_value=500000.0))
        session.commit()

    third = await send_notifications(settings)
    assert third.sent == 1
    assert third.channels[0].sent == 1


@respx.mock
async def test_failed_delivery_is_retried_next_run(stored: Settings):
    """Was nicht ankam, darf nicht als gemeldet gelten."""
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))
    settings = _enable_webhook(stored)

    failed = await send_notifications(settings)
    assert not failed.ok
    assert failed.sent == 0
    assert "Webhook" in (failed.channels[0].error or "")

    with session_scope(settings.database_url) as session:
        assert session.query(NotificationRecord).count() == 0

    route.mock(return_value=httpx.Response(200))
    retry = await send_notifications(settings)
    assert retry.ok
    assert retry.sent >= 2


async def test_dry_run_delivers_nothing_and_remembers_nothing(stored: Settings):
    settings = _enable_webhook(stored)
    report = await send_notifications(settings, dry_run=True)

    assert report.dry_run
    assert report.events
    assert report.sent == 0
    with session_scope(settings.database_url) as session:
        assert session.query(NotificationRecord).count() == 0


async def test_without_a_channel_nothing_is_marked_as_sent(stored: Settings):
    """Sonst waere die erste echte Mail leer - der Bestand gilt dann als gemeldet."""
    report = await send_notifications(stored)
    assert report.events
    assert report.channels == []
    with session_scope(stored.database_url) as session:
        assert session.query(NotificationRecord).count() == 0


async def test_unknown_channel_is_rejected(stored: Settings):
    with pytest.raises(ConfigError, match="Unbekannte"):
        await send_notifications(stored, only_channels=["telegramm"])


async def test_unknown_kind_in_config_is_rejected(stored: Settings):
    stored.notifications.kinds = ["new", "unfug"]
    with pytest.raises(ConfigError, match="Ereignisart"):
        await send_notifications(stored)


async def test_disabled_notifications_do_nothing(stored: Settings):
    stored.notifications.enabled = False
    report = await send_notifications(stored)
    assert report.events == []
    assert report.channels == []


# --- Mailkanal ----------------------------------------------------------------
class _FakeSMTP:
    """Minimaler SMTP-Ersatz - prueft den Ablauf, nicht die Bibliothek."""

    instances: list[_FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: int = 0) -> None:
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.messages: list[object] = []
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def send_message(self, message: object) -> None:
        self.messages.append(message)


async def test_email_is_sent_with_a_readable_digest(
    stored: Settings, monkeypatch: pytest.MonkeyPatch
):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    stored.notifications.email.enabled = True
    stored.notifications.email.recipients = ["einkauf@example.org"]

    report = await send_notifications(stored)

    assert report.ok and report.sent >= 2
    smtp = _FakeSMTP.instances[0]
    assert smtp.started_tls  # starttls ist Default
    message = smtp.messages[0]
    body = message.get_content()  # type: ignore[attr-defined]
    assert "Monitoren" in body
    assert "Die Freigabe bleibt Handarbeit" in body
    assert message["Subject"].startswith("tender-ai:")  # type: ignore[index]


async def test_email_without_recipient_fails_loudly(
    stored: Settings, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    stored.notifications.email.enabled = True
    stored.notifications.email.recipients = []

    report = await send_notifications(stored)
    assert not report.ok
    assert "Empfaenger" in (report.channels[0].error or "")


# --- Darstellung --------------------------------------------------------------
def test_text_digest_lists_every_event_with_its_tender(stored: Settings):
    with session_scope(stored.database_url) as session:
        events = collect_events(
            TenderRepository(session, stored.dedup), kinds=["new"], deadline_days=[7]
        )

    text = render_text(events)
    for event in events:
        assert event.tender_id in text
        assert event.title in text
    assert summary_line(events).startswith("2 neu")

    payload = render_payload(events)
    assert payload["count"] == len(events)
    assert {entry["tender_id"] for entry in payload["events"]} == {"ted:1", "ted:2"}


def test_empty_digest_says_so():
    assert "Keine neuen Meldungen" in render_text([])
    assert summary_line([]) == "Keine neuen Meldungen."
