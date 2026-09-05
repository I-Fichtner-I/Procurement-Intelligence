"""Stufe 6: Freigabe-Gate, Protokoll, Entwurf und Pipeline-Uebersicht."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tender_ai.cli import app
from tender_ai.config import Settings, load_settings
from tender_ai.core.errors import ConfigError
from tender_ai.database.models import TenderDocumentRecord
from tender_ai.database.repository import TenderRepository
from tender_ai.database.session import session_scope
from tender_ai.models.decision import DecisionKind
from tender_ai.models.document import ExtractedDocument, ExtractedTable
from tender_ai.services import (
    analyze_tender,
    approval_state,
    calculate_tender,
    create_offer_draft,
    extract_tender_items,
    pipeline_status,
    record_decision,
    research_and_store,
    run_search,
)
from tender_ai.sources.base import SearchQuery

runner = CliRunner()

PRICE_LIST = (
    "Artikelnummer;Bezeichnung;Hersteller;Typ;Preis;Waehrung;Preisbasis;MwSt;Einheit;Lieferant\n"
    "MX27;Monitor 27 Zoll IPS;Muster GmbH;MX-27;180,00;EUR;netto;19;STK;Lieferant A\n"
    "DS47;Dockingstation USB-C;Docking AG;DS-4711;90,00;EUR;netto;19;STK;Lieferant A\n"
)

LV_TABLE = ExtractedTable(
    page=1,
    header=["Pos.", "Bezeichnung", "Menge", "ME"],
    rows=[
        ["1.10", "Monitor 27 Zoll, Fabrikat: Muster GmbH, Typ: MX-27", "100", "Stk"],
        ["1.20", "Dockingstation USB-C, Fabrikat: Docking AG, Typ: DS-4711", "100", "Stk"],
    ],
)


@pytest.fixture
def prepared(settings: Settings, sample_tender_file: Path, tmp_path: Path) -> Settings:
    sample_tender_file.write_text(
        json.dumps(
            {
                "tenders": [
                    {
                        "source_id": "ap-1",
                        "title": "Lieferung von Bildschirmarbeitsplaetzen",
                        "contracting_authority": "Musterstadt",
                        "national_id": "VG-2026-9",
                        "country": "DEU",
                        "status": "OPEN",
                        "submission_deadline": "2036-10-15T12:00:00+02:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    price_list = tmp_path / "preise.csv"
    price_list.write_text(PRICE_LIST, encoding="utf-8")
    config = yaml.safe_load(settings.config_file.read_text(encoding="utf-8"))
    config["price_sources"] = {"liste": {"type": "catalog", "path": str(price_list)}}
    config["calculation"] = {"markup_percent": 25.0, "overhead_percent": 0.0}
    settings.config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    return load_settings(settings.config_file)


async def _through_calculation(settings: Settings) -> str:
    await run_search(settings, SearchQuery(max_results=5), only_sources=["fixture"])
    tender_id = "fixture:ap-1"
    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, settings.dedup)
        document = TenderDocumentRecord(
            tender_id=tender_id, name="LV.pdf", access="PUBLIC", media_type="application/pdf"
        )
        session.add(document)
        session.flush()
        repository.save_extract(
            document,
            ExtractedDocument(source_path="LV.pdf", file_name="LV.pdf", tables=[LV_TABLE]),
        )
        session.commit()
    await extract_tender_items(settings, tender_id, fetch_missing=False)
    await research_and_store(settings, tender_id)
    calculate_tender(settings, tender_id)
    return tender_id


# --------------------------------------------------------------------------
# Das Gate
# --------------------------------------------------------------------------


async def test_no_draft_without_approval(prepared: Settings):
    """Der Kern der Stufe: ohne Freigabe entsteht kein Dokument."""
    tender_id = await _through_calculation(prepared)

    with pytest.raises(ConfigError) as excinfo:
        create_offer_draft(prepared, tender_id)

    assert "ohne Freigabe" in str(excinfo.value)


async def test_no_draft_after_rejection(prepared: Settings):
    tender_id = await _through_calculation(prepared)
    record_decision(prepared, tender_id, DecisionKind.REJECTED, decided_by="pruefer")

    with pytest.raises(ConfigError):
        create_offer_draft(prepared, tender_id)


async def test_approval_cannot_precede_a_calculation(prepared: Settings):
    """Ohne Zahlen gibt es nichts freizugeben."""
    await run_search(prepared, SearchQuery(max_results=5), only_sources=["fixture"])

    with pytest.raises(ConfigError) as excinfo:
        record_decision(prepared, "fixture:ap-1", DecisionKind.APPROVED, decided_by="pruefer")

    assert "calculate" in str(excinfo.value)


async def test_draft_is_written_after_approval(prepared: Settings, tmp_path: Path):
    tender_id = await _through_calculation(prepared)
    record_decision(prepared, tender_id, DecisionKind.APPROVED, decided_by="pruefer")

    result = create_offer_draft(prepared, tender_id, destination=tmp_path / "entwuerfe")

    assert len(result.files) == 2
    assert all(path.exists() and path.name.startswith("ENTWURF-") for path in result.files)
    assert result.draft.approved_by == "pruefer"
    assert result.as_dict()["is_submitted"] is False


# --------------------------------------------------------------------------
# Veraltete Freigaben
# --------------------------------------------------------------------------


async def test_approval_goes_stale_when_the_calculation_changes(prepared: Settings):
    """Eine alte Zustimmung darf keine neue Rechnung tragen."""
    tender_id = await _through_calculation(prepared)
    record_decision(prepared, tender_id, DecisionKind.APPROVED, decided_by="pruefer")
    assert approval_state(prepared, tender_id).allows_draft

    # Aufschlag aendern -> andere Summe -> die Freigabe deckt sie nicht mehr.
    config = yaml.safe_load(prepared.config_file.read_text(encoding="utf-8"))
    config["calculation"]["markup_percent"] = 40.0
    prepared.config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    changed = load_settings(prepared.config_file)
    calculate_tender(changed, tender_id)

    state = approval_state(changed, tender_id)
    assert state.is_stale
    assert not state.allows_draft
    assert any("geaendert" in blocker for blocker in state.blockers)
    with pytest.raises(ConfigError):
        create_offer_draft(changed, tender_id)


async def test_recalculating_without_changes_keeps_the_approval(prepared: Settings):
    """Ein erneuter Lauf mit denselben Zahlen entwertet nichts."""
    tender_id = await _through_calculation(prepared)
    record_decision(prepared, tender_id, DecisionKind.APPROVED, decided_by="pruefer")

    calculate_tender(prepared, tender_id)

    state = approval_state(prepared, tender_id)
    assert not state.is_stale
    assert state.allows_draft


async def test_renewed_approval_unblocks_the_draft(prepared: Settings, tmp_path: Path):
    tender_id = await _through_calculation(prepared)
    record_decision(prepared, tender_id, DecisionKind.APPROVED, decided_by="pruefer")
    config = yaml.safe_load(prepared.config_file.read_text(encoding="utf-8"))
    config["calculation"]["markup_percent"] = 40.0
    prepared.config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    changed = load_settings(prepared.config_file)
    calculate_tender(changed, tender_id)

    record_decision(changed, tender_id, DecisionKind.APPROVED, decided_by="pruefer")

    assert approval_state(changed, tender_id).allows_draft
    assert create_offer_draft(changed, tender_id, destination=tmp_path).files


# --------------------------------------------------------------------------
# Protokoll
# --------------------------------------------------------------------------


async def test_decisions_are_kept_as_a_history(prepared: Settings):
    tender_id = await _through_calculation(prepared)
    record_decision(prepared, tender_id, DecisionKind.ON_HOLD, decided_by="a", note="Rueckfrage")
    record_decision(prepared, tender_id, DecisionKind.APPROVED, decided_by="b", note="geklaert")

    with session_scope(prepared.database_url) as session:
        repository = TenderRepository(session, prepared.dedup)
        history = repository.decisions_for(tender_id)
        assert len(history) == 2
        assert history[0].kind == "APPROVED"  # neueste zuerst
        assert history[1].note == "Rueckfrage"
        assert repository.get(tender_id).user_decision == "APPROVED"


async def test_decision_records_the_numbers_it_was_based_on(prepared: Settings):
    tender_id = await _through_calculation(prepared)

    decision = record_decision(prepared, tender_id, DecisionKind.APPROVED, decided_by="pruefer")

    assert decision.verdict_at_decision
    assert decision.sale_total_at_decision is not None
    assert decision.calculation_fingerprint


async def test_approval_against_the_recommendation_is_flagged(prepared: Settings):
    """Der Mensch darf ueberstimmen - aber nicht unbemerkt."""
    tender_id = await _through_calculation(prepared)
    # Die Kalkulation urteilt hier UNSUITABLE oder NOT_ASSESSABLE, weil das
    # Risiko mangels Analyse offen ist.
    decision = record_decision(prepared, tender_id, DecisionKind.APPROVED, decided_by="pruefer")
    assert decision.override_warning


# --------------------------------------------------------------------------
# Uebersicht
# --------------------------------------------------------------------------


async def test_pipeline_shows_the_next_step(prepared: Settings):
    """Die Uebersicht nennt die erste offene Stufe - bis ein Mensch entscheidet."""
    await run_search(prepared, SearchQuery(max_results=5), only_sources=["fixture"])
    rows = pipeline_status(prepared, limit=10)
    assert rows and rows[0].next_step == "documents"

    tender_id = await _through_calculation(prepared)
    # Die Analyse wurde uebersprungen - genau darauf zeigt die Uebersicht.
    assert pipeline_status(prepared, limit=10)[0].next_step == "analyze"

    await analyze_tender(prepared, tender_id, fetch_missing=False)
    assert pipeline_status(prepared, limit=10)[0].next_step == "decide"

    record_decision(prepared, tender_id, DecisionKind.APPROVED, decided_by="pruefer")
    # Ab hier steht die Entscheidung ueber allem Uebrigen.
    assert pipeline_status(prepared, limit=10)[0].next_step == "offer"


async def test_pipeline_asks_for_a_new_decision_when_stale(prepared: Settings):
    tender_id = await _through_calculation(prepared)
    record_decision(prepared, tender_id, DecisionKind.APPROVED, decided_by="pruefer")
    config = yaml.safe_load(prepared.config_file.read_text(encoding="utf-8"))
    config["calculation"]["markup_percent"] = 40.0
    prepared.config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    changed = load_settings(prepared.config_file)
    calculate_tender(changed, tender_id)

    row = pipeline_status(changed, limit=10)[0]
    assert row.is_stale
    assert row.next_step == "decide (erneut)"


async def test_rejected_tender_needs_no_next_step(prepared: Settings):
    tender_id = await _through_calculation(prepared)
    record_decision(prepared, tender_id, DecisionKind.REJECTED, decided_by="pruefer")
    assert pipeline_status(prepared, limit=10)[0].next_step == "-"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


async def test_cli_offer_refuses_without_approval(prepared: Settings):
    tender_id = await _through_calculation(prepared)

    result = runner.invoke(app, ["offer", tender_id, "--config", str(prepared.config_file)])

    assert result.exit_code == 1
    assert "ohne Freigabe" in result.output


async def test_cli_decide_and_offer(prepared: Settings, tmp_path: Path):
    tender_id = await _through_calculation(prepared)

    decided = runner.invoke(
        app,
        [
            "decide",
            tender_id,
            "--config",
            str(prepared.config_file),
            "--approve",
            "--by",
            "pruefer",
            "--note",
            "Preise bestaetigt",
            "--json",
        ],
    )
    assert decided.exit_code == 0, decided.output
    assert json.loads(decided.stdout)["kind"] == "APPROVED"

    drafted = runner.invoke(
        app,
        [
            "offer",
            tender_id,
            "--config",
            str(prepared.config_file),
            "--out",
            str(tmp_path),
            "--format",
            "md",
            "--json",
        ],
    )
    assert drafted.exit_code == 0, drafted.output
    payload = json.loads(drafted.stdout)
    assert payload["is_draft"] is True
    assert payload["is_submitted"] is False
    assert len(payload["files"]) == 1


async def test_cli_decide_shows_the_state_without_options(prepared: Settings):
    tender_id = await _through_calculation(prepared)
    result = runner.invoke(
        app, ["decide", tender_id, "--config", str(prepared.config_file), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["kind"] == "PENDING"


async def test_cli_decide_rejects_two_decisions_at_once(prepared: Settings):
    tender_id = await _through_calculation(prepared)
    result = runner.invoke(
        app,
        ["decide", tender_id, "--config", str(prepared.config_file), "--approve", "--reject"],
    )
    assert result.exit_code == 1
    assert "genau eine" in result.output


async def test_cli_offer_rejects_an_unknown_format(prepared: Settings):
    tender_id = await _through_calculation(prepared)
    result = runner.invoke(
        app,
        ["offer", tender_id, "--config", str(prepared.config_file), "--format", "pdf"],
    )
    assert result.exit_code == 1
    assert "Unbekanntes Format" in result.output


async def test_cli_status_lists_the_pipeline(prepared: Settings):
    await _through_calculation(prepared)
    result = runner.invoke(app, ["status", "--config", str(prepared.config_file), "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert rows[0]["stages"]["kalkuliert"] is True
    assert rows[0]["stages"]["analysiert"] is False
    assert rows[0]["next_step"] == "analyze"
