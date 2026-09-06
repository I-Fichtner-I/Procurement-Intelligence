"""Der Takt ueber alle Stufen: tender-ai pipeline.

Geprueft wird, was im Betrieb zaehlt: die Reihenfolge stimmt, ein Ausfall
beendet den Takt nicht, ein zweiter Lauf fasst nichts unveraendertes an - und
die Kette endet vor der Freigabe.
"""

from __future__ import annotations

import pytest

from tender_ai.config import Settings
from tender_ai.core.errors import ConfigError
from tender_ai.database.repository import TenderRepository
from tender_ai.database.session import session_scope
from tender_ai.services import pipeline as pipeline_module
from tender_ai.services import pipeline_status, run_pipeline
from tender_ai.services.pipeline import STAGES, resolve_stages


def test_stage_order_is_fixed_regardless_of_input():
    """Die Reihenfolge ergibt sich aus den Abhaengigkeiten, nicht aus der Eingabe."""
    assert resolve_stages(None) == list(STAGES)
    assert resolve_stages(["calculate", "search"]) == ["search", "calculate"]
    assert resolve_stages(["items", "items"]) == ["items"]


def test_unknown_stage_is_rejected_with_the_known_names():
    with pytest.raises(ConfigError, match="Unbekannte Stufe"):
        resolve_stages(["preise"])


def test_pipeline_stops_before_the_human_decision():
    """Freigabe und Angebot sind bewusst nicht Teil des Takts."""
    assert "decide" not in STAGES
    assert "offer" not in STAGES
    assert STAGES[-1] == "calculate"


async def test_pipeline_runs_the_chain_and_stores_results(settings: Settings):
    report = await run_pipeline(settings, only_sources=["fixture"], limit=10)

    assert report.ok
    assert [stage.name for stage in report.stages] == list(STAGES)
    search = report.stages[0]
    assert search.processed == 2
    assert search.details["new"] == 2

    with session_scope(settings.database_url) as session:
        assert TenderRepository(session, settings.dedup).count(only_primary=False) == 2

    # Die Uebersicht kennt die Ausschreibungen danach.
    rows = pipeline_status(settings)
    assert len(rows) == 2
    assert all(row.stages["recherchiert"] for row in rows)


async def test_second_run_skips_what_has_not_changed(settings: Settings):
    await run_pipeline(settings, only_sources=["fixture"], limit=10)
    second = await run_pipeline(settings, only_sources=["fixture"], limit=10)

    by_stage = {stage.name: stage for stage in second.stages}
    assert by_stage["search"].details["new"] == 0
    # Analyse und Positionen liefen bereits - der taegliche Lauf wiederholt sie nicht.
    assert by_stage["analyze"].processed == 0
    assert by_stage["items"].processed == 0


async def test_force_reprocesses_unchanged_tenders(settings: Settings):
    await run_pipeline(settings, only_sources=["fixture"], limit=10)
    forced = await run_pipeline(settings, only_sources=["fixture"], limit=10, force=True)

    by_stage = {stage.name: stage for stage in forced.stages}
    assert by_stage["analyze"].processed == 2
    assert by_stage["items"].processed == 2


async def test_failing_stage_does_not_end_the_run(settings: Settings, monkeypatch):
    """Faellt eine Stufe aus, arbeiten die spaeteren auf dem Bestand weiter."""

    async def broken(*_args, **_kwargs):
        raise RuntimeError("Analysedienst nicht erreichbar")

    monkeypatch.setattr(pipeline_module, "analyze_open_tenders", broken)
    report = await run_pipeline(settings, only_sources=["fixture"], limit=10)

    assert not report.ok
    by_stage = {stage.name: stage for stage in report.stages}
    assert by_stage["analyze"].ok is False
    assert "Analysedienst nicht erreichbar" in by_stage["analyze"].error
    # Die Recherche davor und die Stufen danach sind trotzdem gelaufen.
    assert by_stage["search"].processed == 2
    assert [stage.name for stage in report.stages] == list(STAGES)
    assert all(stage.ok for stage in report.stages if stage.name != "analyze")


async def test_selected_stages_run_alone(settings: Settings):
    report = await run_pipeline(settings, only_sources=["fixture"], stages=["search"], limit=10)
    assert [stage.name for stage in report.stages] == ["search"]
    assert report.as_dict()["stages"][0]["label"] == "Recherche"
