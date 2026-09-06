"""Meldungen in eine Form bringen, die ein Mensch (oder ein Endpunkt) liest.

Bewusst zwei Darstellungen und keine dritte: reiner Text fuer die Mail und im
Terminal, JSON fuer den Webhook. Kein HTML - eine Mail, die als Text ankommt,
ist in jedem Client lesbar, und der Inhalt hier ist eine Liste, kein Layout.
"""

from __future__ import annotations

from typing import Any

from ..models.common import display
from .events import KIND_LABELS, NotificationEvent


def group_by_kind(events: list[NotificationEvent]) -> dict[str, list[NotificationEvent]]:
    grouped: dict[str, list[NotificationEvent]] = {}
    for event in events:
        grouped.setdefault(event.kind, []).append(event)
    return grouped


def summary_line(events: list[NotificationEvent]) -> str:
    """Eine Zeile, die schon in der Betreffzeile oder Vorschau traegt."""
    if not events:
        return "Keine neuen Meldungen."
    grouped = group_by_kind(events)
    parts = [
        f"{len(grouped[kind])} {KIND_LABELS.get(kind, kind).lower()}"
        for kind in KIND_LABELS
        if kind in grouped
    ]
    return ", ".join(parts)


def render_text(events: list[NotificationEvent]) -> str:
    """Textfassung: nach Art gruppiert, innerhalb nach Dringlichkeit."""
    if not events:
        return "Keine neuen Meldungen.\n"

    lines: list[str] = [summary_line(events), ""]
    grouped = group_by_kind(events)
    for kind, label in KIND_LABELS.items():
        entries = grouped.get(kind)
        if not entries:
            continue
        lines.append(f"{label} ({len(entries)})")
        lines.append("-" * len(f"{label} ({len(entries)})"))
        for event in entries:
            frist = (
                f"[{event.deadline_days} T] " if event.deadline_days is not None else "[Frist ?] "
            )
            lines.append(f"{frist}{event.title}")
            lines.append(f"    {event.detail}")
            lines.append(f"    {event.tender_id}")
            if event.url:
                lines.append(f"    {event.url}")
            lines.append("")
        lines.append("")

    lines.append("Naechster Schritt: tender-ai status")
    lines.append("Die Freigabe bleibt Handarbeit - dieses Programm entscheidet nichts.")
    return "\n".join(lines).rstrip() + "\n"


def render_payload(events: list[NotificationEvent]) -> dict[str, Any]:
    """JSON fuer den Webhook - dieselben Angaben, maschinenlesbar."""
    return {
        "summary": summary_line(events),
        "count": len(events),
        "events": [event.as_dict() for event in events],
    }


def subject_for(template: str, events: list[NotificationEvent]) -> str:
    """Betreffzeile aus der Vorlage; unbekannte Platzhalter bleiben stehen."""
    try:
        return template.format(count=len(events), summary=summary_line(events))
    except (KeyError, IndexError):
        return display(template)
