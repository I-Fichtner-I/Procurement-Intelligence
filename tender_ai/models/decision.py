"""Freigabe und Angebotsentwurf (Stufe 6).

Hier endet die Automatik. Der Auftrag schreibt den Ablauf fest:

    Analyse -> FREIGABE DURCH NUTZER -> Angebotsentwurf -> manuelle Pruefung
    -> manuelle Abgabe

Das Tool erzeugt einen **Entwurf**. Es gibt kein Angebot ab, es versendet
nichts, es bestaetigt nichts. Drei Eigenschaften halten diese Grenze:

1. **Ohne Freigabe kein Entwurf.** Der Entwurf entsteht erst, nachdem ein
   Mensch entschieden hat - nicht als Vorschau, nicht "zur Sicherheit schon
   mal".
2. **Die Freigabe gilt Zahlen, nicht einer Ausschreibung.** Sie haelt fest,
   welche Kalkulation freigegeben wurde. Aendert sich danach etwas an Preisen
   oder Positionen, ist die Freigabe **veraltet** und muss erneuert werden -
   sonst traegt eine alte Zustimmung eine neue Rechnung.
3. **Jede Entscheidung bleibt nachvollziehbar.** Wer, wann, warum - als
   Historie, nicht als ueberschriebenes Feld.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .common import display, utcnow


class DecisionKind(StrEnum):
    """Entscheidung eines Menschen ueber eine Ausschreibung."""

    PENDING = "PENDING"  # noch nicht entschieden
    APPROVED = "APPROVED"  # zur Angebotserstellung freigegeben
    REJECTED = "REJECTED"  # verworfen
    ON_HOLD = "ON_HOLD"  # zurueckgestellt, offene Fragen


class UserDecision(BaseModel):
    """Eine Freigabeentscheidung - der Eintrag im Protokoll."""

    model_config = ConfigDict(use_enum_values=False)

    tender_id: str
    kind: DecisionKind
    decided_by: str
    decided_at: datetime = Field(default_factory=utcnow)
    note: str | None = None

    #: Zustand der Kalkulation, auf den sich die Freigabe bezieht. Aendert er
    #: sich, gilt die Freigabe nicht mehr fuer die neuen Zahlen.
    calculation_fingerprint: str | None = None
    verdict_at_decision: str | None = None
    margin_percent_at_decision: float | None = None
    sale_total_at_decision: float | None = None
    currency: str | None = None
    #: Gesetzt, wenn gegen die Empfehlung des Werkzeugs freigegeben wurde. Der
    #: Mensch darf ueberstimmen - aber nicht versehentlich.
    override_warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tender_id": self.tender_id,
            "kind": str(self.kind),
            "decided_by": self.decided_by,
            "decided_at": self.decided_at.isoformat(),
            "note": self.note,
            "verdict_at_decision": display(self.verdict_at_decision),
            "margin_percent_at_decision": self.margin_percent_at_decision,
            "sale_total_at_decision": self.sale_total_at_decision,
            "currency": display(self.currency),
            "override_warning": self.override_warning,
        }


class ApprovalState(BaseModel):
    """Aktueller Freigabestand einer Ausschreibung."""

    model_config = ConfigDict(use_enum_values=False)

    tender_id: str
    kind: DecisionKind = DecisionKind.PENDING
    decision: UserDecision | None = None
    #: True, wenn sich die Kalkulation seit der Freigabe geaendert hat.
    is_stale: bool = False
    #: Warum der Entwurf (noch) nicht erstellt werden darf.
    blockers: list[str] = Field(default_factory=list)

    @property
    def allows_draft(self) -> bool:
        """Darf ein Angebotsentwurf erzeugt werden?"""
        return self.kind is DecisionKind.APPROVED and not self.is_stale and not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "tender_id": self.tender_id,
            "kind": str(self.kind),
            "is_stale": self.is_stale,
            "allows_draft": self.allows_draft,
            "blockers": self.blockers,
            "decision": self.decision.as_dict() if self.decision else None,
        }


class DraftPosition(BaseModel):
    """Eine Zeile des Angebotsentwurfs."""

    position: str | None = None
    title: str
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    total_price: float | None = None
    #: Woher der Preis stammt - im Entwurf sichtbar, damit er pruefbar bleibt.
    source_note: str | None = None
    #: True, wenn die Zeile von Hand ergaenzt werden muss.
    needs_manual_entry: bool = False


#: Platzhalter fuer Angaben, die das Tool nicht kennen kann. Bewusst auffaellig:
#: ein Entwurf mit sichtbaren Luecken wird nicht versehentlich abgegeben.
PLACEHOLDER = "<<BITTE AUSFUELLEN>>"


class OfferDraft(BaseModel):
    """Angebotsentwurf - ein Arbeitsdokument, kein Angebot."""

    model_config = ConfigDict(use_enum_values=False)

    tender_id: str
    tender_title: str | None = None
    contracting_authority: str | None = None
    national_id: str | None = None
    submission_deadline: datetime | None = None

    positions: list[DraftPosition] = Field(default_factory=list)
    net_total: float | None = None
    currency: str | None = None

    #: Angaben, die vor der Abgabe zu ergaenzen sind.
    placeholders: list[str] = Field(default_factory=list)
    #: Was vor der Abgabe zu pruefen ist.
    review_notes: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)
    approved_by: str | None = None
    approved_at: datetime | None = None

    @property
    def manual_position_count(self) -> int:
        return sum(1 for position in self.positions if position.needs_manual_entry)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tender_id": self.tender_id,
            "tender_title": display(self.tender_title),
            "contracting_authority": display(self.contracting_authority),
            "national_id": display(self.national_id),
            "submission_deadline": (
                self.submission_deadline.isoformat() if self.submission_deadline else None
            ),
            "position_count": len(self.positions),
            "manual_position_count": self.manual_position_count,
            "net_total": self.net_total,
            "currency": display(self.currency),
            "placeholders": self.placeholders,
            "review_notes": self.review_notes,
            "generated_at": self.generated_at.isoformat(),
            "approved_by": display(self.approved_by),
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            # Unmissverstaendlich in jeder maschinellen Ausgabe:
            "is_draft": True,
            "is_submitted": False,
            "requires_manual_submission": True,
        }
