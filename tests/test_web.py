"""Stufe 9: die lokale Weboberflaeche.

Der Schwerpunkt liegt nicht auf dem Aussehen, sondern auf den Auflagen aus dem
Architektur-Review (F-30): **Auth, CSRF und Nachvollziehbarkeit kommen mit dem
ersten schreibenden Endpunkt.** Dazu die Regel des Projekts, dass Fremdtexte
nie ungefiltert in die Ausgabe geraten.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tender_ai.config import Settings
from tender_ai.core.errors import ConfigError
from tender_ai.database.repository import TenderRepository
from tender_ai.database.session import session_scope
from tender_ai.models.tender import Tender, TenderStatus
from tender_ai.web import create_app
from tender_ai.web.render import money, pending
from tender_ai.web.security import CSRF_COOKIE, TOKEN_COOKIE, is_loopback

LOCAL = ("127.0.0.1", 4711)
REMOTE = ("192.0.2.10", 4711)
DEADLINE = datetime.now(UTC) + timedelta(days=20)


def tender(**overrides) -> Tender:
    base = dict(
        id="ted:1",
        source="ted",
        source_id="1",
        source_url="https://ted.test.invalid/1",
        title="Lieferung von 2.000 Monitoren",
        contracting_authority="Musterstadt",
        country="DEU",
        submission_deadline=DEADLINE,
        estimated_value=420000.0,
        currency="EUR",
        status=TenderStatus.OPEN,
    )
    base.update(overrides)
    return Tender(**base)


@pytest.fixture
def stored(settings: Settings) -> Settings:
    with session_scope(settings.database_url) as session:
        TenderRepository(session, settings.dedup).upsert(tender())
        session.commit()
    return settings


@pytest.fixture
def client(stored: Settings) -> TestClient:
    return TestClient(create_app(stored), client=LOCAL)


# --- Zugang -------------------------------------------------------------------
def test_without_a_token_the_ui_stays_on_this_machine(stored: Settings):
    """Ohne Token ist die Oberflaeche nur lokal - auch hinter einem Proxy."""
    remote = TestClient(create_app(stored), client=REMOTE)
    response = remote.get("/")
    assert response.status_code == 403
    assert "Kein Zugriff" in response.text


def test_binding_beyond_localhost_without_a_token_refuses_to_start(stored: Settings):
    stored.web.host = "0.0.0.0"  # noqa: S104 - genau dieser Fall soll scheitern
    with pytest.raises(ConfigError, match="Zugangstoken"):
        create_app(stored)


def test_binding_beyond_localhost_is_allowed_with_a_token(stored: Settings):
    stored.web.host = "0.0.0.0"  # noqa: S104
    stored.web_token = SecretStr("geheim")
    app = create_app(stored)  # kein Fehler
    assert app.state.token == "geheim"


def test_token_protects_every_view(stored: Settings):
    stored.web_token = SecretStr("geheim")
    client = TestClient(create_app(stored), client=REMOTE, follow_redirects=False)

    assert client.get("/").status_code == 303  # zur Anmeldung
    assert client.get("/login").status_code == 200
    assert client.get("/health").status_code == 200  # Betriebspruefung bleibt offen

    wrong = client.post("/login", data={"token": "falsch"})
    assert wrong.status_code == 401
    assert TOKEN_COOKIE not in wrong.cookies

    good = client.post("/login", data={"token": "geheim"})
    assert good.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


def test_token_also_works_as_a_bearer_header(stored: Settings):
    stored.web_token = SecretStr("geheim")
    client = TestClient(create_app(stored), client=REMOTE)
    assert client.get("/", headers={"Authorization": "Bearer geheim"}).status_code == 200
    # Ein falscher Token landet auf der Anmeldung, nicht in der Uebersicht.
    wrong = client.get("/", headers={"Authorization": "Bearer falsch"})
    assert "Anmeldung" in wrong.text


def test_loopback_detection():
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("127.0.1.5")
    assert not is_loopback("192.0.2.10")
    assert not is_loopback("0.0.0.0")  # noqa: S104
    assert not is_loopback(None)


# --- Ansichten ----------------------------------------------------------------
def test_overview_lists_open_tenders(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "Lieferung von 2.000 Monitoren" in response.text
    assert "/tender/ted:1" in response.text


def test_detail_shows_the_state_and_the_open_blockers(client: TestClient):
    response = client.get("/tender/ted:1")
    assert response.status_code == 200
    assert "Musterstadt" in response.text
    assert "420.000 EUR" in response.text
    # Ohne Kalkulation ist die Freigabe gesperrt - und sagt auch warum.
    assert "Keine Kalkulation vorhanden" in response.text
    # Was noch nicht gelaufen ist, heisst nicht "UNKNOWN", sondern sagt es.
    assert "noch nicht bewertet" in response.text


def test_unknown_tender_is_a_404(client: TestClient):
    assert client.get("/tender/gibt-es-nicht").status_code == 404


def test_health_reports_version_and_count(client: TestClient):
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["tenders"] == 1


def test_titles_from_sources_are_escaped(settings: Settings):
    """Ein Titel von einem fremden Portal darf kein Skript einschleusen."""
    with session_scope(settings.database_url) as session:
        TenderRepository(session, settings.dedup).upsert(
            tender(title="<script>alert('xss')</script> Monitore")
        )
        session.commit()

    client = TestClient(create_app(settings), client=LOCAL)
    body = client.get("/tender/ted:1").text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


# --- Freigabe -----------------------------------------------------------------
def _csrf(client: TestClient) -> str:
    client.get("/tender/ted:1")
    return client.cookies[CSRF_COOKIE]


def test_decision_is_recorded_with_the_name_of_who_decided(client: TestClient):
    token = _csrf(client)
    response = client.post(
        "/tender/ted:1/decide",
        data={
            "kind": "ON_HOLD",
            "decided_by": "Justin Fichtner",
            "note": "warte auf Unterlagen",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    page = client.get("/tender/ted:1").text
    assert "Justin Fichtner" in page
    assert "warte auf Unterlagen" in page
    assert "zurueckgestellt" in page


def test_a_decision_without_a_valid_csrf_token_is_refused(client: TestClient):
    _csrf(client)
    response = client.post(
        "/tender/ted:1/decide",
        data={"kind": "APPROVED", "decided_by": "Fremd", "csrf_token": "geraten"},
    )
    assert response.status_code == 400
    assert "nicht mehr gueltig" in response.text
    # Und nichts steht im Protokoll.
    assert "Fremd" not in client.get("/tender/ted:1").text


def test_approval_without_a_calculation_is_rejected_with_a_reason(client: TestClient):
    """Die Oberflaeche darf die Regel der Stufe 6 nicht aushebeln."""
    token = _csrf(client)
    response = client.post(
        "/tender/ted:1/decide",
        data={"kind": "APPROVED", "decided_by": "Justin", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "calculate" in response.headers["location"]
    assert "freigegeben" not in client.get("/tender/ted:1").text


def test_unknown_decision_kind_changes_nothing(client: TestClient):
    token = _csrf(client)
    response = client.post(
        "/tender/ted:1/decide",
        data={"kind": "VIELLEICHT", "decided_by": "Justin", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Unbekannte" in response.headers["location"]


# --- Darstellung --------------------------------------------------------------
def test_money_is_formatted_the_german_way():
    assert money(420000.0, "EUR") == "420.000 EUR"
    assert money(1234.5, "EUR") == "1.234,50 EUR"
    assert money(None) == "UNKNOWN"


def test_pending_says_what_is_missing_instead_of_unknown():
    assert "noch nicht bewertet" in pending(None, hint="noch nicht bewertet")
    assert pending(3, hint="egal") == "3"


# --- Kalkulierte Ausschreibung: Zahlen, Kriterien, veraltete Freigabe ----------
def _with_calculation(settings: Settings, *, verdict: str = "INTERESTING", sale: float = 60000.0):
    """Eine Ausschreibung mit Kalkulation - so, wie Stufe 5 sie hinterlaesst."""
    from tender_ai.database.models import CalculationRecord

    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, settings.dedup)
        record = repository.get("ted:1")
        calculation = session.get(CalculationRecord, "ted:1") or CalculationRecord(
            tender_id="ted:1"
        )
        calculation.verdict = verdict
        calculation.score = 78
        calculation.coverage_percent = 100
        calculation.currency = "EUR"
        calculation.cost_total = 48000.0
        calculation.sale_total = sale
        calculation.margin_percent = 20.0
        calculation.content_hash = record.content_hash
        calculation.criteria = [
            {
                "code": "margin",
                "label": "Mindestmarge",
                "required": ">= 15 %",
                "actual": "20 %",
                "passed": True,
                "undetermined": False,
            },
            {
                "code": "risk",
                "label": "Hoechstrisiko",
                "required": "<= 40",
                "actual": "UNKNOWN",
                "passed": False,
                "undetermined": True,
            },
        ]
        session.add(calculation)
        session.commit()


def test_calculated_tender_shows_figures_and_criteria(client: TestClient, stored: Settings):
    _with_calculation(stored)
    body = client.get("/tender/ted:1").text

    assert "60.000 EUR" in body  # Angebotspreis, deutsche Schreibweise
    assert "48.000 EUR" in body  # Selbstkosten
    assert "Mindestmarge" in body
    assert "erfuellt" in body
    # Ein unbestimmtes Kriterium gilt nie als erfuellt.
    assert "Hoechstrisiko" in body
    assert "offen" in body


def test_approval_goes_stale_when_the_calculation_changes(client: TestClient, stored: Settings):
    """Eine alte Zustimmung darf keine neue Rechnung tragen (Stufe 6, hier sichtbar)."""
    _with_calculation(stored)
    token = _csrf(client)
    approved = client.post(
        "/tender/ted:1/decide",
        data={"kind": "APPROVED", "decided_by": "Justin", "csrf_token": token},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    assert "freigegeben" in client.get("/tender/ted:1").text

    # Die Zahlen aendern sich - die Freigabe deckt sie nicht mehr.
    _with_calculation(stored, sale=75000.0)
    body = client.get("/tender/ted:1").text
    assert "Freigabe veraltet" in body or "erneut freigeben" in body


def test_changes_are_listed_in_the_detail_view(client: TestClient, stored: Settings):
    with session_scope(stored.database_url) as session:
        repository = TenderRepository(session, stored.dedup)
        repository.upsert(tender(title="Lieferung von 3.000 Monitoren"))
        session.commit()

    body = client.get("/tender/ted:1").text
    assert "Aenderungen" in body
    assert "3.000 Monitoren" in body


# --- Entwurf aus der Oberflaeche ----------------------------------------------
def test_draft_button_appears_only_after_approval(client: TestClient, stored: Settings):
    """Ohne Freigabe kein Entwurf - auch nicht 'nur zur Ansicht'."""
    body = client.get("/tender/ted:1").text
    assert "Angebotsentwurf" in body
    assert "erst nach der Freigabe" in body
    assert "Entwurf erzeugen" not in body

    _with_calculation(stored)
    token = _csrf(client)
    client.post(
        "/tender/ted:1/decide",
        data={"kind": "APPROVED", "decided_by": "Justin", "csrf_token": token},
    )
    assert "Entwurf erzeugen" in client.get("/tender/ted:1").text


def test_draft_is_created_and_can_be_read(client: TestClient, stored: Settings):
    _with_calculation(stored)
    token = _csrf(client)
    client.post(
        "/tender/ted:1/decide",
        data={"kind": "APPROVED", "decided_by": "Justin", "csrf_token": token},
    )

    created = client.post("/tender/ted:1/draft", data={"csrf_token": token}, follow_redirects=False)
    assert created.status_code == 303
    assert "Entwurf+erzeugt" in created.headers["location"]

    files = list((stored.data_dir / "offers").glob("ENTWURF-ted-1-*"))
    assert {path.suffix for path in files} == {".md", ".xlsx"}

    markdown = client.get("/tender/ted:1/draft.md")
    assert markdown.status_code == 200
    assert "ENTWURF" in markdown.text
    # Der Entwurf nennt sich selbst so - und ist kein Angebot.
    assert "Monitoren" in markdown.text


def test_draft_without_approval_is_refused_by_the_service(client: TestClient, stored: Settings):
    _with_calculation(stored)
    token = _csrf(client)
    response = client.post(
        "/tender/ted:1/draft", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "Freigabe" in response.headers["location"]
    # Es entsteht auch keine Datei, die spaeter jemand fuer ein Angebot haelt.
    offers = stored.data_dir / "offers"
    assert not offers.is_dir() or not list(offers.glob("*"))


def test_draft_needs_a_csrf_token(client: TestClient, stored: Settings):
    _with_calculation(stored)
    token = _csrf(client)
    client.post(
        "/tender/ted:1/decide",
        data={"kind": "APPROVED", "decided_by": "Justin", "csrf_token": token},
    )
    response = client.post("/tender/ted:1/draft", data={"csrf_token": "geraten"})
    assert response.status_code == 400


def test_draft_view_without_a_file_says_so(client: TestClient):
    response = client.get("/tender/ted:1/draft.md", follow_redirects=False)
    assert response.status_code == 303
    assert "Noch+kein+Entwurf" in response.headers["location"]


# --- Suche und Filter ---------------------------------------------------------
def test_search_narrows_the_overview(client: TestClient, stored: Settings):
    with session_scope(stored.database_url) as session:
        TenderRepository(session, stored.dedup).upsert(
            tender(id="ted:2", source_id="2", title="Wartung von Aufzugsanlagen")
        )
        session.commit()

    alle = client.get("/").text
    assert "Monitoren" in alle and "Aufzugsanlagen" in alle

    treffer = client.get("/?q=aufzug").text
    assert "Aufzugsanlagen" in treffer
    assert "Lieferung von 2.000 Monitoren" not in treffer

    leer = client.get("/?q=gibtesnicht").text
    assert "Nichts gefunden" in leer


def test_filter_todo_shows_what_waits_for_a_decision(client: TestClient, stored: Settings):
    # Ohne Kalkulation wartet nichts auf eine Entscheidung.
    assert "Nichts gefunden" in client.get("/?filter=todo").text

    _with_calculation(stored)
    assert "Monitoren" in client.get("/?filter=todo").text

    token = _csrf(client)
    client.post(
        "/tender/ted:1/decide",
        data={"kind": "REJECTED", "decided_by": "Justin", "csrf_token": token},
    )
    assert "Nichts gefunden" in client.get("/?filter=todo").text
    assert "Monitoren" in client.get("/?filter=decided").text


def test_price_research_is_shown_in_the_detail_view(client: TestClient, stored: Settings):
    """Stufe 4 hinterlaesst einen Datensatz - die Detailansicht muss ihn lesen koennen.

    Die Ansicht hat einmal ein Feld angesprochen, das es am Datensatz nie gab;
    aufgefallen ist das erst an einer echten Ausschreibung mit Preisbild.
    """
    from tender_ai.database.models import PriceResearchRecord

    with session_scope(stored.database_url) as session:
        session.add(
            PriceResearchRecord(
                tender_id="ted:1",
                item_count=4,
                usable_count=3,
                coverage_percent=75,
                sources_used=["beispiel_liste"],
            )
        )
        session.commit()

    response = client.get("/tender/ted:1")
    assert response.status_code == 200
    assert "3/4 kalkulierbar" in response.text
