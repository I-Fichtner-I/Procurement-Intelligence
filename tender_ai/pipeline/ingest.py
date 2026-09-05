"""Rechercherlauf: Quellen abfragen, Ergebnisse vereinheitlichen und speichern.

Robustheitsregel: Der Ausfall einer Quelle beendet nie den Gesamtlauf. Jede
Quelle wird einzeln gekapselt, Fehler werden protokolliert und im Bericht
ausgewiesen. Die Suche laeuft parallel, die Persistenz sequenziell in
Prioritaetsreihenfolge - so wird bei Dubletten zuverlaessig die hoeher
priorisierte Quelle zur Primaerquelle.

Persistenz je Datensatz in einem Savepoint: ein fehlerhafter Datensatz kostet
genau diesen Datensatz, nie die bereits gespeicherten derselben Quelle.

Die Datenbankarbeit ist blockierend und laeuft deshalb in einem Thread
(``asyncio.to_thread``): waehrend eine Quelle gespeichert wird, bleibt der
Event-Loop fuer die uebrigen Abrufe und Timeouts ansprechbar. Ueber die
Threadgrenze gehen nur einfache Werte - Berichte und IDs, nie ORM-Objekte.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session
from structlog.contextvars import bound_contextvars

from ..config import Settings
from ..core.http import HttpClient
from ..core.logging import get_logger
from ..database.repository import TenderRepository, UpsertResult
from ..models.tender import Tender
from ..sources.base import SearchQuery, TenderSource

log = get_logger(__name__)

#: Oeffnet eine neue Session; die Persistenz schliesst sie selbst wieder.
SessionFactory = Callable[[], Session]


@dataclass(slots=True)
class SourceReport:
    name: str
    type: str
    ok: bool = True
    found: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    failed: int = 0
    failed_ids: list[str] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "type": self.type,
            "ok": self.ok,
            "found": self.found,
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "failed_ids": list(self.failed_ids),
            "error": self.error,
            "duration_seconds": round(self.duration_seconds, 2),
        }


@dataclass(slots=True)
class IngestReport:
    sources: list[SourceReport] = field(default_factory=list)
    tenders: list[Tender] = field(default_factory=list)
    new_tender_ids: list[str] = field(default_factory=list)
    updated_tender_ids: list[str] = field(default_factory=list)
    #: Fehler einzelner Datensaetze: {"source", "tender_id", "error"}
    record_errors: list[dict[str, Any]] = field(default_factory=list)
    http_stats: dict[str, Any] = field(default_factory=dict)
    stored: bool = True

    @property
    def found(self) -> int:
        return sum(report.found for report in self.sources)

    @property
    def new(self) -> int:
        return sum(report.new for report in self.sources)

    @property
    def updated(self) -> int:
        return sum(report.updated for report in self.sources)

    @property
    def duplicates(self) -> int:
        return sum(report.duplicates for report in self.sources)

    @property
    def failed(self) -> int:
        return sum(report.failed for report in self.sources)

    @property
    def errors(self) -> list[dict[str, Any]]:
        """Quellfehler und Datensatzfehler gemeinsam - fuer Laufprotokoll und CLI."""
        source_errors = [
            {"source": report.name, "error": report.error}
            for report in self.sources
            if not report.ok
        ]
        return source_errors + list(self.record_errors)

    @property
    def source_errors(self) -> list[dict[str, Any]]:
        return [
            {"source": report.name, "error": report.error}
            for report in self.sources
            if not report.ok
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "new": self.new,
            "updated": self.updated,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "stored": self.stored,
            "sources": [report.as_dict() for report in self.sources],
            "errors": self.errors,
            "http": self.http_stats,
        }


@dataclass(slots=True)
class PersistOutcome:
    """Ergebnis der Persistenz einer Quelle - bewusst ohne ORM-Objekte.

    Nur einfache Werte gehen ueber die Threadgrenze zurueck in den Event-Loop.
    """

    report: SourceReport
    new_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    record_errors: list[dict[str, Any]] = field(default_factory=list)


class IngestService:
    """Orchestriert einen Rechercherlauf.

    Entweder ``session`` (eine laufende Transaktion, z. B. in Tests) oder
    ``session_factory`` (Regelfall: jede Schreibeinheit oeffnet ihre eigene
    Session im Thread) - nie beides.
    """

    def __init__(
        self,
        settings: Settings,
        sources: Sequence[TenderSource],
        http: HttpClient,
        session: Session | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        if session is not None and session_factory is not None:
            raise ValueError("entweder session oder session_factory angeben, nicht beides")
        self.settings = settings
        self.sources = list(sources)
        self.http = http
        self.session = session
        self.session_factory = session_factory
        self.repository = self._repository(session) if session is not None else None

    @property
    def persists(self) -> bool:
        """Kann dieser Lauf ueberhaupt speichern?"""
        return self.session is not None or self.session_factory is not None

    def _repository(self, session: Session) -> TenderRepository:
        return TenderRepository(
            session,
            dedup_config=self.settings.dedup,
            source_priority={name: cfg.priority for name, cfg in self.settings.sources.items()},
        )

    @contextmanager
    def _unit_of_work(self) -> Iterator[TenderRepository]:
        """Repository fuer eine abgeschlossene Schreibeinheit.

        Mit ``session_factory`` bekommt jede Einheit ihre eigene Session und
        gibt sie am Ende wieder frei - so haelt kein Vorgang eine
        Schreibtransaktion offen, waehrend eine andere Einheit schreibt.
        """
        if self.session is not None:
            assert self.repository is not None
            yield self.repository
            self.session.commit()
            return
        assert self.session_factory is not None
        session = self.session_factory()
        try:
            yield self._repository(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def _search_one(
        self, source: TenderSource, query: SearchQuery
    ) -> tuple[TenderSource, list[Tender] | Exception, float]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with bound_contextvars(source=source.name):
            try:
                results = await source.search(query)
            except Exception as exc:  # noqa: BLE001 - eine Quelle darf nie den Gesamtlauf beenden
                log.error("source_failed", source=source.name, error=str(exc))
                return source, exc, loop.time() - started
            return source, results, loop.time() - started

    # --- Datenbankarbeit (laeuft ueber ``asyncio.to_thread``) ----------------
    def _begin_run(self, query: SearchQuery) -> int | None:
        with self._unit_of_work() as repository:
            stale = repository.mark_stale_runs()
            if stale:
                log.warning("stale_runs_aborted", runs=stale)
            run = repository.start_run(
                [source.name for source in self.sources],
                {
                    "keywords": query.keywords,
                    "cpv_codes": query.cpv_codes,
                    "countries": query.countries,
                    "published_after": str(query.published_after),
                    "max_results": query.max_results,
                },
            )
            return run.id

    def _persist_source(
        self, source_name: str, source_type: str, tenders: Sequence[Tender]
    ) -> PersistOutcome:
        """Alle Treffer einer Quelle speichern - je Datensatz ein Savepoint."""
        outcome = PersistOutcome(
            report=SourceReport(name=source_name, type=source_type, found=len(tenders))
        )
        source_report = outcome.report
        with self._unit_of_work() as repository:
            for tender in tenders:
                try:
                    # Bei einer Exception rollt SQLAlchemy nur den Savepoint
                    # zurueck; die vorher geflushten Datensaetze bleiben.
                    with repository.session.begin_nested():
                        result: UpsertResult = repository.upsert(tender)
                except Exception as exc:  # noqa: BLE001 - ein defekter Datensatz kostet nur sich selbst
                    log.error("persist_failed", tender=tender.id, error=str(exc))
                    source_report.failed += 1
                    source_report.failed_ids.append(tender.id)
                    outcome.record_errors.append(
                        {"source": source_name, "tender_id": tender.id, "error": str(exc)}
                    )
                    continue

                # Zaehler erst nach erfolgreichem Savepoint erhoehen.
                if result.action == "new":
                    source_report.new += 1
                    outcome.new_ids.append(result.record.id)
                elif result.action == "updated":
                    source_report.updated += 1
                    outcome.updated_ids.append(result.record.id)
                    log.info(
                        "tender_changed",
                        tender=result.record.id,
                        changes=[change[0] for change in result.changes],
                    )
                elif result.action == "duplicate":
                    source_report.duplicates += 1
                    log.info(
                        "duplicate_detected",
                        tender=result.record.id,
                        duplicate_of=result.duplicate_of,
                        reason=result.duplicate_reason,
                        confidence=result.duplicate_confidence,
                    )
                else:
                    source_report.unchanged += 1
        return outcome

    def _record_source_state(
        self,
        name: str,
        source_type: str,
        *,
        success: bool,
        result_count: int = 0,
        error: str | None = None,
    ) -> None:
        with self._unit_of_work() as repository:
            repository.update_source_state(
                name, source_type, success=success, result_count=result_count, error=error
            )

    def _finish_run(self, run_id: int, report: IngestReport) -> None:
        with self._unit_of_work() as repository:
            repository.finish_run(
                run_id,
                found=report.found,
                new=report.new,
                updated=report.updated,
                duplicates=report.duplicates,
                errors=report.errors,
                http_stats=report.http_stats,
            )

    def _abort_run(self, run_id: int, error: str) -> None:
        with self._unit_of_work() as repository:
            repository.abort_run(run_id, error=error)

    async def run(
        self,
        query: SearchQuery,
        *,
        store: bool = True,
        download_documents: bool = False,
    ) -> IngestReport:
        report = IngestReport(stored=store and self.persists)
        if not self.sources:
            log.warning("no_sources_enabled")
            return report

        run_id: int | None = None
        if self.persists and store:
            run_id = await asyncio.to_thread(self._begin_run, query)

        # run_id auch ohne Speicherung binden: die Logzeilen eines Laufs
        # bleiben so zusammen auswertbar (--no-store, doctor).
        with bound_contextvars(run_id=run_id if run_id is not None else f"dry-{uuid4().hex[:8]}"):
            try:
                await self._collect(query, report, store=store, downloads=download_documents)
            except Exception as exc:
                # Ein abgebrochener Lauf wird als solcher protokolliert, statt
                # bis zum naechsten Start als "laeuft" stehen zu bleiben.
                if run_id is not None:
                    await asyncio.to_thread(self._abort_run, run_id, f"{type(exc).__name__}: {exc}")
                log.error("ingest_aborted", error=str(exc))
                raise

            if run_id is not None:
                await asyncio.to_thread(self._finish_run, run_id, report)

            log.info(
                "ingest_done",
                found=report.found,
                new=report.new,
                updated=report.updated,
                duplicates=report.duplicates,
                failed=report.failed,
                errors=len(report.source_errors),
            )
        return report

    async def _collect(
        self, query: SearchQuery, report: IngestReport, *, store: bool, downloads: bool
    ) -> None:
        """Quellen parallel abfragen, Treffer sequenziell nach Prioritaet speichern."""
        results = await asyncio.gather(
            *(self._search_one(source, query) for source in self.sources)
        )

        for source, outcome, duration in sorted(results, key=lambda item: item[0].priority):
            with bound_contextvars(source=source.name):
                if isinstance(outcome, Exception):
                    source_report = SourceReport(
                        name=source.name,
                        type=source.type_name,
                        ok=False,
                        error=f"{type(outcome).__name__}: {outcome}",
                        duration_seconds=duration,
                    )
                    if self.persists:
                        await asyncio.to_thread(
                            self._record_source_state,
                            source.name,
                            source.type_name,
                            success=False,
                            error=source_report.error,
                        )
                    report.sources.append(source_report)
                    continue

                tenders = outcome
                report.tenders.extend(tenders)

                if downloads:
                    await self._download_documents(source, tenders)

                if self.persists and store:
                    persisted = await asyncio.to_thread(
                        self._persist_source, source.name, source.type_name, tenders
                    )
                    source_report = persisted.report
                    report.new_tender_ids.extend(persisted.new_ids)
                    report.updated_tender_ids.extend(persisted.updated_ids)
                    report.record_errors.extend(persisted.record_errors)
                else:
                    source_report = SourceReport(
                        name=source.name, type=source.type_name, found=len(tenders)
                    )
                source_report.duration_seconds = duration

                if self.persists:
                    await asyncio.to_thread(
                        self._record_source_state,
                        source.name,
                        source.type_name,
                        success=True,
                        result_count=len(tenders),
                    )
                report.sources.append(source_report)

        report.http_stats = self.http.stats.as_dict()

    async def _download_documents(self, source: TenderSource, tenders: Sequence[Tender]) -> None:
        destination = Path(self.settings.documents_dir)
        for tender in tenders:
            try:
                await source.download_documents(tender, destination)
            except Exception as exc:  # noqa: BLE001 - Downloadfehler duerfen die Recherche nicht stoppen
                log.warning(
                    "documents_failed", source=source.name, tender=tender.id, error=str(exc)
                )
