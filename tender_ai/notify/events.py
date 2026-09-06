"""Was ist seit dem letzten Mal passiert, das jemand wissen muss?

Vier Ereignisarten, jede mit einem eigenen Grund:

``new``       eine Ausschreibung ist neu im Bestand
``changed``   an einer bekannten hat sich etwas geaendert (Frist, Volumen,
              Status, Dokumente) - genau die Felder, die eine Entscheidung
              umwerfen koennen
``deadline``  die Frist naehert sich einer Schwelle (7, 3, 1 Tage)
``decision``  eine Kalkulation liegt vor, das Urteil ist gut, und niemand hat
              bisher entschieden

Jedes Ereignis traegt einen ``dedupe_key``: er beschreibt, was genau einmal
gemeldet werden soll. Eine neue Ausschreibung einmal; jede Aenderung einmal
(ueber ihre ID); jede Fristschwelle einmal; eine wartende Entscheidung erneut,
sobald sich die Zahlen aendern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from ..database.models import CalculationRecord, DecisionRecord, TenderRecord
from ..database.repository import TenderRepository
from ..models.calculation import Verdict
from ..models.common import display

#: Urteile, die eine Entscheidung wert sind. Ein "unbrauchbar" muss niemanden
#: aus dem Feierabend holen.
WORTH_DECIDING = (str(Verdict.VERY_INTERESTING), str(Verdict.INTERESTING), str(Verdict.REVIEW))

KINDS: tuple[str, ...] = ("new", "changed", "deadline", "decision")

#: Anzeigenamen der Ereignisarten.
KIND_LABELS: dict[str, str] = {
    "new": "Neu",
    "changed": "Geaendert",
    "deadline": "Frist",
    "decision": "Entscheidung faellig",
}


@dataclass(slots=True)
class NotificationEvent:
    """Eine Meldung - fertig zum Rendern, ohne Datenbankzugriff."""

    tender_id: str
    kind: str
    title: str
    #: Ein Satz, der sagt, was passiert ist.
    detail: str
    dedupe_key: str
    deadline_days: int | None = None
    url: str | None = None
    #: Sortierschluessel: kleiner = dringender.
    urgency: int = 100
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tender_id": self.tender_id,
            "kind": self.kind,
            "label": self.label,
            "title": self.title,
            "detail": self.detail,
            "deadline_days": self.deadline_days,
            "url": self.url,
            **self.extra,
        }


def _days_until(deadline: datetime | None) -> int | None:
    if deadline is None:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return (deadline - datetime.now(UTC)).days


def _is_open(record: TenderRecord) -> bool:
    days = _days_until(record.submission_deadline)
    return days is None or days >= 0


def collect_events(
    repository: TenderRepository,
    *,
    kinds: list[str],
    deadline_days: list[int],
    limit: int = 200,
) -> list[NotificationEvent]:
    """Alle meldenswerten Ereignisse einsammeln, dringendste zuerst.

    Was davon schon gemeldet wurde, entscheidet der Aufrufer je Kanal - hier
    wird nur gesammelt.
    """
    events: list[NotificationEvent] = []
    wanted = set(kinds)

    if "new" in wanted or "deadline" in wanted or "decision" in wanted:
        records = repository.list_tenders(limit=limit, open_only=True, order_by="deadline")
        for record in records:
            days = _days_until(record.submission_deadline)
            title = display(record.title)
            if "new" in wanted:
                events.append(
                    NotificationEvent(
                        tender_id=record.id,
                        kind="new",
                        title=title,
                        detail=_new_detail(record, days),
                        dedupe_key=f"new:{record.id}",
                        deadline_days=days,
                        url=record.source_url,
                        urgency=days if days is not None else 999,
                    )
                )
            if "deadline" in wanted and days is not None:
                threshold = threshold_for(days, deadline_days)
                if threshold is not None:
                    events.append(
                        NotificationEvent(
                            tender_id=record.id,
                            kind="deadline",
                            title=title,
                            detail=(
                                f"Frist laeuft in {days} Tag(en) ab "
                                f"({display(record.submission_deadline)})."
                            ),
                            dedupe_key=f"deadline:{record.id}:{threshold}",
                            deadline_days=days,
                            url=record.source_url,
                            urgency=days,
                        )
                    )

        if "decision" in wanted:
            events.extend(_decision_events(repository, limit=limit))

    if "changed" in wanted:
        events.extend(_change_events(repository, limit=limit))

    events.sort(key=lambda event: (event.urgency, event.tender_id, event.kind))
    return events


def _new_detail(record: TenderRecord, days: int | None) -> str:
    parts = [f"Neue Ausschreibung ({record.source})"]
    if record.contracting_authority:
        parts.append(display(record.contracting_authority))
    if days is not None:
        parts.append(f"Frist in {days} Tag(en)")
    if record.estimated_value:
        currency = display(record.currency, missing="")
        parts.append(f"{record.estimated_value:,.0f} {currency}".strip())
    return " · ".join(parts) + "."


def threshold_for(days: int, thresholds: list[int]) -> int | None:
    """Die kleinste erreichte Schwelle - so wird je Schwelle genau einmal erinnert."""
    reached = [value for value in sorted(thresholds) if days <= value]
    return reached[0] if reached else None


def _change_events(repository: TenderRepository, *, limit: int) -> list[NotificationEvent]:
    changes = repository.recent_changes(limit=limit)
    events: list[NotificationEvent] = []
    for change in changes:
        record = change.tender
        if record is None or not _is_open(record):
            continue  # abgelaufene Ausschreibungen sind keine Meldung mehr wert
        days = _days_until(record.submission_deadline)
        events.append(
            NotificationEvent(
                tender_id=change.tender_id,
                kind="changed",
                title=display(record.title),
                detail=(
                    f"{change.field}: {display(change.old_value)} -> {display(change.new_value)}"
                ),
                dedupe_key=f"change:{change.id}",
                deadline_days=days,
                url=record.source_url,
                urgency=days if days is not None else 999,
                extra={"field": change.field},
            )
        )
    return events


def _decision_events(repository: TenderRepository, *, limit: int) -> list[NotificationEvent]:
    """Kalkuliert, brauchbares Urteil, aber niemand hat entschieden."""
    stmt = (
        select(CalculationRecord, TenderRecord)
        .join(TenderRecord, TenderRecord.id == CalculationRecord.tender_id)
        .outerjoin(DecisionRecord, DecisionRecord.tender_id == CalculationRecord.tender_id)
        .where(
            CalculationRecord.verdict.in_(WORTH_DECIDING),
            DecisionRecord.id.is_(None),
            TenderRecord.submission_deadline.is_(None)
            | (TenderRecord.submission_deadline >= datetime.now(UTC)),
        )
        .limit(limit)
    )
    events: list[NotificationEvent] = []
    for calculation, record in repository.session.execute(stmt):
        days = _days_until(record.submission_deadline)
        margin = calculation.margin_percent
        events.append(
            NotificationEvent(
                tender_id=record.id,
                kind="decision",
                title=display(record.title),
                detail=(
                    f"Kalkuliert: {calculation.verdict}"
                    + (f", Marge {margin:.1f} %" if margin is not None else "")
                    + " - wartet auf eine Entscheidung (tender-ai decide)."
                ),
                # Aendert sich die Rechnung, ist die Meldung neu faellig.
                dedupe_key=f"decision:{record.id}:{_calculation_key(calculation)}",
                deadline_days=days,
                url=record.source_url,
                urgency=days if days is not None else 999,
                extra={"verdict": calculation.verdict, "score": calculation.score},
            )
        )
    return events


def _calculation_key(calculation: CalculationRecord) -> str:
    sale = calculation.sale_total
    return (
        f"{calculation.verdict}:{calculation.score}:{sale:.2f}"
        if sale is not None
        else (f"{calculation.verdict}:{calculation.score}:-")
    )
