"""Stufe 6: Freigabe, Angebotsentwurf und Pipeline-Uebersicht.

Der Auftrag schreibt den Ablauf fest: Analyse -> Freigabe durch den Nutzer ->
Angebotsentwurf -> manuelle Pruefung -> manuelle Abgabe. Dieser Dienst haelt
die Freigabestufe.

Er gibt nichts ab. Er erzeugt Dateien und schreibt Protokoll - mehr nicht.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..core.errors import ConfigError
from ..core.logging import get_logger
from ..database.models import CalculationRecord, DecisionRecord
from ..database.repository import TenderRepository
from ..database.session import session_scope
from ..models.calculation import (
    CriterionResult,
    PositionCost,
    Scenario,
    TenderCalculation,
    Verdict,
)
from ..models.decision import (
    ApprovalState,
    DecisionKind,
    OfferDraft,
    UserDecision,
)
from ..offer import build_draft, draft_filename, write_markdown, write_xlsx

log = get_logger(__name__)


def calculation_fingerprint(record: CalculationRecord) -> str:
    """Kennzeichen der freigegebenen Zahlen.

    Bewusst ueber die Ergebnisse und nicht ueber den Zeitstempel: eine erneute
    Kalkulation mit unveraenderten Zahlen soll eine Freigabe nicht entwerten,
    eine geaenderte Summe schon.
    """
    parts = [
        record.verdict,
        str(record.score),
        str(record.coverage_percent),
        f"{record.cost_total:.2f}" if record.cost_total is not None else "-",
        f"{record.sale_total:.2f}" if record.sale_total is not None else "-",
        f"{record.margin_percent:.4f}" if record.margin_percent is not None else "-",
        str(record.position_count),
        str(record.calculated_count),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _to_decision(record: DecisionRecord) -> UserDecision:
    return UserDecision(
        tender_id=record.tender_id,
        kind=DecisionKind(record.kind),
        decided_by=record.decided_by,
        decided_at=record.decided_at,
        note=record.note,
        calculation_fingerprint=record.calculation_fingerprint,
        verdict_at_decision=record.verdict_at_decision,
        margin_percent_at_decision=record.margin_percent_at_decision,
        sale_total_at_decision=record.sale_total_at_decision,
        currency=record.currency,
    )


def _to_calculation(record: CalculationRecord) -> TenderCalculation:
    """Gespeicherte Kalkulation zurueck in das Modell - fuer den Entwurf."""
    calculation = TenderCalculation(
        tender_id=record.tender_id,
        verdict=Verdict(record.verdict),
        score=record.score,
        coverage_percent=record.coverage_percent,
        currency=record.currency,
        warnings=list(record.warnings or []),
        review_notes=list(record.review_notes or []),
        calculated_at=record.calculated_at,
    )
    calculation.positions = [
        PositionCost(
            position=None if entry.get("position") in (None, "UNKNOWN") else entry["position"],
            title=entry.get("title", ""),
            quantity=entry.get("quantity"),
            unit=None if entry.get("unit") in (None, "UNKNOWN") else entry["unit"],
            unit_purchase_price=entry.get("unit_purchase_price"),
            supplier=None if entry.get("supplier") in (None, "UNKNOWN") else entry["supplier"],
            match_confidence=entry.get("match_confidence"),
            purchase_total=entry.get("purchase_total"),
            shipping_total=entry.get("shipping_total") or 0.0,
            surcharge_total=entry.get("surcharge_total") or 0.0,
            cost_total=entry.get("cost_total"),
            sale_total=entry.get("sale_total"),
            margin_absolute=entry.get("margin_absolute"),
            margin_percent=entry.get("margin_percent"),
            warnings=list(entry.get("warnings") or []),
        )
        for entry in (record.positions or [])
    ]
    calculation.scenarios = [
        Scenario.model_validate(
            {**entry, "kind": entry.get("kind", "EXPECTED")},
        )
        for entry in (record.scenarios or [])
        if isinstance(entry, dict)
    ]
    calculation.criteria = [
        CriterionResult.model_validate(entry)
        for entry in (record.criteria or [])
        if isinstance(entry, dict)
    ]
    return calculation


def approval_state(settings: Settings, tender_id: str) -> ApprovalState:
    """Aktueller Freigabestand - inklusive der Gruende, die einen Entwurf sperren."""
    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, settings.dedup)
        record = repository.get(tender_id)
        if record is None:
            raise ConfigError(f"Ausschreibung nicht gefunden: {tender_id}")

        latest = repository.latest_decision(record.id)
        state = ApprovalState(
            tender_id=record.id,
            kind=DecisionKind(latest.kind) if latest else DecisionKind.PENDING,
            decision=_to_decision(latest) if latest else None,
        )

        calculation = repository.calculation_for(record.id)
        if calculation is None:
            state.blockers.append(
                "Keine Kalkulation vorhanden - zuerst 'tender-ai calculate' ausfuehren."
            )
            return state

        if latest is not None and latest.kind == str(DecisionKind.APPROVED):
            current = calculation_fingerprint(calculation)
            if latest.calculation_fingerprint and latest.calculation_fingerprint != current:
                # Eine alte Zustimmung darf keine neue Rechnung tragen.
                state.is_stale = True
                state.blockers.append(
                    "Die Kalkulation hat sich seit der Freigabe geaendert - bitte erneut freigeben."
                )
        if state.kind is not DecisionKind.APPROVED:
            state.blockers.append(
                f"Nicht freigegeben (Stand: {state.kind}) - "
                f"'tender-ai decide {record.id} --approve' entscheidet."
            )
        return state


def record_decision(
    settings: Settings,
    tender_id: str,
    kind: DecisionKind,
    *,
    decided_by: str,
    note: str | None = None,
) -> UserDecision:
    """Entscheidung eines Menschen festhalten - als Protokolleintrag."""
    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, settings.dedup)
        record = repository.get(tender_id)
        if record is None:
            raise ConfigError(f"Ausschreibung nicht gefunden: {tender_id}")

        calculation = repository.calculation_for(record.id)
        if kind is DecisionKind.APPROVED and calculation is None:
            raise ConfigError(
                "Ohne Kalkulation gibt es nichts freizugeben - "
                f"zuerst 'tender-ai calculate {record.id}' ausfuehren."
            )

        decision = UserDecision(
            tender_id=record.id,
            kind=kind,
            decided_by=decided_by,
            note=note,
            calculation_fingerprint=(calculation_fingerprint(calculation) if calculation else None),
            verdict_at_decision=calculation.verdict if calculation else None,
            margin_percent_at_decision=calculation.margin_percent if calculation else None,
            sale_total_at_decision=calculation.sale_total if calculation else None,
            currency=calculation.currency if calculation else None,
        )
        # Eine Freigabe gegen die eigene Empfehlung ist zulaessig - der Mensch
        # entscheidet. Sie soll aber nicht unbemerkt passieren.
        if kind is DecisionKind.APPROVED and calculation is not None:
            if calculation.verdict == str(Verdict.NOT_ASSESSABLE):
                decision.override_warning = (
                    f"Freigabe trotz nicht bewertbarer Datenlage "
                    f"(Abdeckung {calculation.coverage_percent} Prozent) - die Zahlen "
                    f"beruhen auf {calculation.calculated_count} von "
                    f"{calculation.position_count} Positionen."
                )
            elif calculation.verdict == str(Verdict.UNSUITABLE):
                failed = [
                    entry.get("label", entry.get("code", "?"))
                    for entry in (calculation.criteria or [])
                    if isinstance(entry, dict) and not entry.get("passed")
                ]
                decision.override_warning = (
                    "Freigabe trotz verletzter Mindestkriterien: "
                    + ", ".join(str(label) for label in failed)
                )
        repository.save_decision(decision, record)
        session.commit()
        log.info(
            "decision_recorded",
            tender=record.id,
            kind=str(kind),
            by=decided_by,
            verdict=decision.verdict_at_decision,
        )
        return decision


@dataclass(slots=True)
class DraftResult:
    """Ergebnis der Entwurfserstellung - Dateien plus Inhalt."""

    draft: OfferDraft
    files: list[Path]

    def as_dict(self) -> dict[str, Any]:
        payload = self.draft.as_dict()
        payload["files"] = [str(path) for path in self.files]
        return payload


def create_offer_draft(
    settings: Settings,
    tender_id: str,
    *,
    destination: Path | None = None,
    formats: tuple[str, ...] = ("md", "xlsx"),
) -> DraftResult:
    """Angebotsentwurf erzeugen - nur nach Freigabe.

    Die Sperre ist der Kern der Stufe: ohne Freigabe entsteht kein Entwurf,
    auch nicht "nur zur Ansicht". Ein Dokument, das aussieht wie ein Angebot,
    soll es ohne menschliche Entscheidung gar nicht erst geben.
    """
    state = approval_state(settings, tender_id)
    if not state.allows_draft:
        raise ConfigError("Kein Angebotsentwurf ohne Freigabe: " + " ".join(state.blockers))

    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, settings.dedup)
        record = repository.get(state.tender_id)
        if record is None:  # pragma: no cover - zwischenzeitlich geloescht
            raise ConfigError(f"Ausschreibung nicht gefunden: {tender_id}")
        tender = TenderRepository.to_tender(record)
        calculation_record = repository.calculation_for(record.id)
        if calculation_record is None:  # pragma: no cover - von approval_state geprueft
            raise ConfigError("Keine Kalkulation vorhanden.")
        calculation = _to_calculation(calculation_record)

    draft = build_draft(tender, calculation, state.decision)
    target_dir = destination or (settings.data_dir / "offers")
    files: list[Path] = []
    if "md" in formats:
        files.append(
            write_markdown(
                draft, target_dir / draft_filename(draft.tender_id, ".md", draft.generated_at)
            )
        )
    if "xlsx" in formats:
        files.append(
            write_xlsx(
                draft, target_dir / draft_filename(draft.tender_id, ".xlsx", draft.generated_at)
            )
        )
    log.info(
        "offer_draft_created",
        tender=draft.tender_id,
        positions=len(draft.positions),
        manual=draft.manual_position_count,
        files=[str(path) for path in files],
    )
    return DraftResult(draft=draft, files=files)


#: Die Stufen der Kette, in ihrer Reihenfolge.
PIPELINE_STAGES = (
    ("recherchiert", "Ausschreibung gefunden"),
    ("unterlagen", "Unterlagen ausgelesen"),
    ("analysiert", "Anforderungen und Risiko bewertet"),
    ("positionen", "Positionen erkannt"),
    ("preise", "Preise recherchiert"),
    ("kalkuliert", "Kosten und Marge berechnet"),
    ("entschieden", "Vom Nutzer entschieden"),
)


@dataclass(slots=True)
class PipelineRow:
    """Stand einer Ausschreibung ueber alle Stufen."""

    tender_id: str
    title: str | None
    deadline_days: int | None
    stages: dict[str, bool]
    verdict: str | None
    decision: str
    is_stale: bool

    @property
    def next_step(self) -> str:
        """Der naechste sinnvolle Befehl - die eigentliche Hilfe der Uebersicht.

        Eine Entscheidung eines Menschen steht ueber den offenen
        Zwischenschritten: wer freigegeben hat, will als Naechstes den Entwurf
        und nicht den Hinweis auf eine uebersprungene Analyse. Dass die Stufe
        fehlt, zeigt die Tabelle daneben ohnehin.
        """
        if self.is_stale:
            return "decide (erneut)"
        if self.decision == str(DecisionKind.APPROVED):
            return "offer"
        if self.decision in (str(DecisionKind.REJECTED), str(DecisionKind.ON_HOLD)):
            return "-"
        if not self.stages["unterlagen"]:
            return "documents"
        if not self.stages["analysiert"]:
            return "analyze"
        if not self.stages["positionen"]:
            return "items"
        if not self.stages["preise"]:
            return "prices"
        if not self.stages["kalkuliert"]:
            return "calculate"
        return "decide"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tender_id": self.tender_id,
            "title": self.title,
            "deadline_days": self.deadline_days,
            "stages": self.stages,
            "verdict": self.verdict,
            "decision": self.decision,
            "is_stale": self.is_stale,
            "next_step": self.next_step,
        }


def pipeline_status(
    settings: Settings, *, limit: int = 50, open_only: bool = True
) -> list[PipelineRow]:
    """Wo steht welche Ausschreibung - und was waere der naechste Schritt?"""
    rows: list[PipelineRow] = []
    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, settings.dedup)
        for record in repository.list_tenders(
            limit=limit, open_only=open_only, order_by="deadline"
        ):
            latest = repository.latest_decision(record.id)
            calculation = record.calculation
            is_stale = bool(
                latest
                and latest.kind == str(DecisionKind.APPROVED)
                and calculation is not None
                and latest.calculation_fingerprint
                and latest.calculation_fingerprint != calculation_fingerprint(calculation)
            )
            tender = TenderRepository.to_tender(record)
            rows.append(
                PipelineRow(
                    tender_id=record.id,
                    title=record.title,
                    deadline_days=tender.days_until_deadline,
                    stages={
                        "recherchiert": True,
                        "unterlagen": bool(repository.extracts_for(record.id)),
                        "analysiert": record.risk_analysis is not None,
                        "positionen": record.item_extraction is not None,
                        "preise": record.price_research is not None,
                        "kalkuliert": calculation is not None,
                        "entschieden": latest is not None,
                    },
                    verdict=calculation.verdict if calculation else None,
                    decision=latest.kind if latest else str(DecisionKind.PENDING),
                    is_stale=is_stale,
                )
            )
    return rows
