"""Die Kette in einem Takt: Recherche, Analyse, Positionen, Preise, Kalkulation.

Bis hierher war jede Stufe ein eigener Befehl - und der cron-Eintrag eine
Liste von Aufrufen, deren Reihenfolge und Fehlerbehandlung der Betreiber im
Kopf haben musste. ``run_pipeline`` nimmt ihm das ab: die Stufen laufen in
der einzig sinnvollen Reihenfolge, jede ueberspringt, was seit dem letzten
Lauf unveraendert ist, und der Ausfall einer Stufe beendet den Takt nicht -
die spaeteren arbeiten dann eben auf dem vorhandenen Bestand weiter.

**Wo der Takt endet:** nach der Kalkulation. Freigabe (``decide``) und
Angebotsentwurf (``offer``) bleiben Handarbeit - der Mensch entscheidet, ob
angeboten wird, und niemand sonst. Ein automatischer Takt, der bis zum
Angebot durchliefe, waere genau die Abkuerzung, die dieses Projekt nicht
nehmen will.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..core.errors import ConfigError
from ..core.logging import get_logger
from ..sources.base import SearchQuery
from .analysis import analyze_open_tenders
from .calculation import calculate_open_tenders
from .items import extract_items_for_open_tenders
from .pricing import research_open_tenders
from .search import run_search

log = get_logger(__name__)

#: Reihenfolge der Stufen. Jede baut auf dem Ergebnis der vorherigen auf:
#: ohne Unterlagen keine Positionen, ohne Positionen keine Preise, ohne
#: Preise keine Kalkulation.
STAGES: tuple[str, ...] = ("search", "analyze", "items", "prices", "calculate")

#: Was die Stufen in der Ausgabe heissen.
STAGE_LABELS: dict[str, str] = {
    "search": "Recherche",
    "analyze": "Analyse",
    "items": "Positionen",
    "prices": "Preise",
    "calculate": "Kalkulation",
}


@dataclass(slots=True)
class StageReport:
    """Ergebnis einer Stufe - auch wenn sie ausgefallen ist."""

    name: str
    ok: bool = True
    #: Erfolgreich verarbeitete Einheiten (Stufe 1: gefundene Ausschreibungen).
    processed: int = 0
    #: Einzelne Ausschreibungen, die in dieser Stufe scheiterten.
    failed: int = 0
    duration_seconds: float = 0.0
    #: Fehler der gesamten Stufe (Konfiguration, Netzwerk) - nicht einzelner Saetze.
    error: str | None = None
    #: Stufenspezifische Zahlen fuer die Ausgabe (z. B. neu/aktualisiert).
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return STAGE_LABELS.get(self.name, self.name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "label": self.label,
            "ok": self.ok,
            "processed": self.processed,
            "failed": self.failed,
            "duration_seconds": round(self.duration_seconds, 2),
            "error": self.error,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class PipelineReport:
    stages: list[StageReport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Lief jede ausgefuehrte Stufe als Ganzes durch?

        Einzelne gescheiterte Ausschreibungen zaehlen hier nicht - sie stehen
        in ``failed`` der jeweiligen Stufe und sind der Normalfall bei
        unvollstaendigen Vergabeunterlagen.
        """
        return all(stage.ok for stage in self.stages)

    @property
    def failed_records(self) -> int:
        return sum(stage.failed for stage in self.stages)

    @property
    def duration_seconds(self) -> float:
        return sum(stage.duration_seconds for stage in self.stages)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "duration_seconds": round(self.duration_seconds, 2),
            "failed_records": self.failed_records,
            "stages": [stage.as_dict() for stage in self.stages],
        }


def resolve_stages(selected: Sequence[str] | None) -> list[str]:
    """Ausgewaehlte Stufen in die feste Reihenfolge bringen.

    Ein Tippfehler faellt hier auf, statt still eine Stufe zu ueberspringen.
    """
    if not selected:
        return list(STAGES)
    unknown = sorted({name for name in selected if name not in STAGES})
    if unknown:
        raise ConfigError(
            f"Unbekannte Stufe(n): {', '.join(unknown)}. Bekannt: {', '.join(STAGES)}."
        )
    wanted = set(selected)
    return [name for name in STAGES if name in wanted]


async def run_pipeline(
    settings: Settings,
    *,
    query: SearchQuery | None = None,
    only_sources: Sequence[str] | None = None,
    stages: Sequence[str] | None = None,
    limit: int = 50,
    fetch_missing: bool = True,
    force: bool = False,
) -> PipelineReport:
    """Alle Stufen bis zur Kalkulation in einem Durchgang.

    ``force`` rechnet auch das neu, was sich seit dem letzten Lauf nicht
    geaendert hat - fuer den Fall, dass Regeln oder Preislisten angepasst
    wurden. Der Regelfall ist der taegliche Lauf ohne ``force``: er fasst nur
    an, was neu oder veraendert ist.
    """
    plan = resolve_stages(stages)
    report = PipelineReport()
    log.info("pipeline_started", stages=plan, limit=limit, force=force)

    for name in plan:
        stage = StageReport(name=name)
        started = time.perf_counter()
        try:
            await _run_stage(
                name,
                stage,
                settings,
                query=query,
                only_sources=only_sources,
                limit=limit,
                fetch_missing=fetch_missing,
                force=force,
            )
        except Exception as exc:  # noqa: BLE001 - eine Stufe beendet nie den Takt
            stage.ok = False
            stage.error = f"{type(exc).__name__}: {exc}"
            log.error("pipeline_stage_failed", stage=name, error=str(exc))
        stage.duration_seconds = time.perf_counter() - started
        report.stages.append(stage)

    log.info(
        "pipeline_done",
        ok=report.ok,
        failed_records=report.failed_records,
        duration=round(report.duration_seconds, 2),
    )
    return report


async def _run_stage(
    name: str,
    stage: StageReport,
    settings: Settings,
    *,
    query: SearchQuery | None,
    only_sources: Sequence[str] | None,
    limit: int,
    fetch_missing: bool,
    force: bool,
) -> None:
    if name == "search":
        search_query = query or SearchQuery.from_config(settings.search)
        result = await run_search(settings, search_query, only_sources=only_sources)
        stage.processed = result.found
        stage.failed = result.failed
        stage.details = {
            "new": result.new,
            "updated": result.updated,
            "duplicates": result.duplicates,
            "sources_failed": len(result.source_errors),
        }
        return

    if name == "analyze":
        analysis = await analyze_open_tenders(
            settings, limit=limit, fetch_missing=fetch_missing, skip_analyzed=not force
        )
        stage.processed = analysis.count
        stage.failed = len(analysis.failed)
        return

    if name == "items":
        items = await extract_items_for_open_tenders(
            settings, limit=limit, fetch_missing=fetch_missing, skip_extracted=not force
        )
        stage.processed = items.count
        stage.failed = len(items.failed)
        stage.details = {"items": items.item_count}
        return

    if name == "prices":
        prices = await research_open_tenders(settings, limit=limit, skip_researched=not force)
        stage.processed = prices.count
        stage.failed = len(prices.failed)
        return

    if name == "calculate":
        # Die Rechnung braucht kein Netzwerk, blockiert aber - deshalb im Thread.
        calculation = await asyncio.to_thread(calculate_open_tenders, settings, limit=limit)
        stage.processed = calculation.count
        stage.failed = len(calculation.failed)
        return

    raise ConfigError(f"Unbekannte Stufe: {name}")  # pragma: no cover - resolve_stages prueft


def run_pipeline_sync(settings: Settings, **kwargs: Any) -> PipelineReport:
    """Synchroner Einstieg fuer CLI und Skripte."""
    return asyncio.run(run_pipeline(settings, **kwargs))
