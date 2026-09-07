"""HTML der Weboberflaeche - von Hand, ohne Template-Engine.

Zwei Seiten und ein Formular rechtfertigen keine weitere Abhaengigkeit. Dafuer
gilt hier eine Regel ohne Ausnahme: **jeder Wert aus der Datenbank geht durch
``esc``**. Ausschreibungstitel kommen von fremden Portalen; ein ``<script>`` im
Titel darf keine Wirkung haben.
"""

from __future__ import annotations

from html import escape
from typing import Any

from ..models.common import display
from ..services.approval import PipelineRow
from .security import CSRF_FIELD

#: Aktenoptik: Amtsblau auf kuehlem Papier, Haarlinien, Randspalte.
STYLE = """
:root {
  --paper: #f3f5f8; --surface: #fff; --ink: #131a24; --muted: #5c6875;
  --rule: #c7d0da; --rule-strong: #97a5b4; --accent: #1d4e89; --accent-soft: #e4ecf6;
  --ok: #2f6b40; --warn: #8a6116; --bad: #9c322b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #0f141a; --surface: #161d25; --ink: #e3e9f0; --muted: #97a4b2;
    --rule: #2a3441; --rule-strong: #3d4b5c; --accent: #7fa9de; --accent-soft: #1a2635;
    --ok: #7fb682; --warn: #d8b268; --bad: #dd8b83;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); line-height: 1.55;
  font-family: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--accent); }
code, .mono { font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, monospace; }
header { border-bottom: 1px solid var(--rule-strong); padding: 1.1rem 0; }
.sheet { max-width: 72rem; margin: 0 auto; padding: 0 1.25rem; }
header .sheet { display: flex; align-items: baseline; gap: 1.25rem; flex-wrap: wrap; }
header h1 { font-size: 1.25rem; margin: 0; letter-spacing: -0.01em; }
header nav { display: flex; gap: 1rem; font-size: 0.9rem; margin-left: auto; }
main { padding: 1.75rem 0 3rem; }
h2 { font-size: 1.35rem; margin: 0 0 0.35rem; }
h3 { font-size: 1rem; margin: 1.75rem 0 0.5rem; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--muted); font-weight: 600; }
p.lede { color: var(--muted); margin: 0 0 1.25rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.92rem; }
th, td { text-align: left; padding: 0.5rem 0.75rem 0.5rem 0;
  border-bottom: 1px solid var(--rule); vertical-align: top; }
th { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.09em; color: var(--muted);
  border-bottom: 1px solid var(--rule-strong); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.wrap { overflow-x: auto; }
.mark-yes { color: var(--ok); } .mark-no { color: var(--muted); }
.pill { display: inline-block; padding: 0.05rem 0.45rem; border: 1px solid currentColor;
  border-radius: 2px; font-size: 0.75rem; letter-spacing: 0.04em; }
.pill-ok { color: var(--ok); } .pill-warn { color: var(--warn); }
.pill-bad { color: var(--bad); }
.pill-open { color: var(--muted); }
dl.facts { display: grid; grid-template-columns: 12rem minmax(0, 1fr); gap: 0.35rem 1rem;
  margin: 0.5rem 0 0; font-size: 0.94rem; }
dl.facts dt { color: var(--muted); } dl.facts dd { margin: 0; }
form.decide { margin-top: 0.75rem; border: 1px solid var(--rule);
  border-left: 2px solid var(--accent); background: var(--surface);
  padding: 1rem; border-radius: 2px; max-width: 46rem; }
form.decide label { display: block; font-size: 0.85rem; color: var(--muted);
  margin-bottom: 0.2rem; }
form.decide input[type=text], form.decide textarea, form.decide select {
  width: 100%; padding: 0.4rem 0.5rem; border: 1px solid var(--rule); border-radius: 2px;
  background: var(--paper); color: var(--ink); font: inherit; margin-bottom: 0.75rem; }
form.decide .buttons { display: flex; gap: 0.5rem; flex-wrap: wrap; }
button { font: inherit; padding: 0.45rem 0.9rem; border-radius: 2px; cursor: pointer;
  border: 1px solid var(--accent); background: var(--accent); color: var(--paper); }
button.secondary { background: transparent; color: var(--accent); }
.note { border-left: 2px solid var(--warn); padding: 0.5rem 0.85rem; margin: 0.75rem 0;
  background: var(--surface); color: var(--muted); font-size: 0.9rem; }
.note-bad { border-left-color: var(--bad); }
.hint { color: var(--muted); font-size: 0.85rem; margin-top: 1.5rem; }
"""


def esc(value: Any, *, missing: str = "UNKNOWN") -> str:
    """Jeder Fremdwert geht hier durch - ohne Ausnahme."""
    return escape(display(value, missing=missing))


def money(value: float | None, currency: str | None = None) -> str:
    """Betrag in deutscher Schreibweise; fehlt er, wird nichts erfunden."""
    if value is None:
        return escape("UNKNOWN")
    amount = f"{value:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
    amount = amount.removesuffix(",00")
    return escape(f"{amount} {currency}".strip() if currency else amount)


def pending(value: Any, *, hint: str) -> str:
    """Fehlt der Wert, weil die Stufe noch nicht lief, sagt das die Zelle.

    "UNKNOWN" waere hier irrefuehrend: die Angabe fehlt nicht, sie ist schlicht
    noch nicht erhoben.
    """
    if value is None:
        return f'<span style="color:var(--muted)">{escape(hint)}</span>'
    return escape(str(value))


def page(title: str, body: str, *, subtitle: str = "") -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{STYLE}</style></head><body>"
        '<header><div class="sheet">'
        f"<h1>{escape(title)}</h1>"
        f'<span class="mono" style="color:var(--muted);font-size:0.85rem">{escape(subtitle)}</span>'
        '<nav><a href="/">Uebersicht</a><a href="/?all=1">inkl. abgelaufene</a></nav>'
        "</div></header>"
        f'<main><div class="sheet">{body}</div></main>'
        "</body></html>"
    )


def _decision_pill(decision: str, *, stale: bool) -> str:
    if stale:
        return '<span class="pill pill-warn">Freigabe veraltet</span>'
    mapping = {
        "APPROVED": ("pill-ok", "freigegeben"),
        "REJECTED": ("pill-bad", "abgelehnt"),
        "ON_HOLD": ("pill-warn", "zurueckgestellt"),
    }
    css, label = mapping.get(decision, ("pill-open", "offen"))
    return f'<span class="pill {css}">{escape(label)}</span>'


def _criterion_pill(item: dict[str, Any]) -> str:
    if item.get("passed"):
        return '<span class="pill pill-ok">erfuellt</span>'
    return '<span class="pill pill-open">offen</span>'


def overview(rows: list[PipelineRow], *, open_only: bool) -> str:
    """Die Liste, mit der jemand seinen Tag beginnt: Frist zuerst."""
    if not rows:
        leer = "Keine laufenden Ausschreibungen." if open_only else "Keine Ausschreibungen."
        return f'<h2>Uebersicht</h2><p class="lede">{leer}</p>'

    stages = (
        ("unterlagen", "Unt."),
        ("analysiert", "Ana."),
        ("positionen", "Pos."),
        ("preise", "Preis"),
        ("kalkuliert", "Kalk."),
    )
    head = "".join(f'<th class="num">{escape(label)}</th>' for _key, label in stages)
    body = []
    for row in rows:
        marks = "".join(
            f'<td class="num {"mark-yes" if row.stages[key] else "mark-no"}">'
            f"{'&#10003;' if row.stages[key] else '&middot;'}</td>"
            for key, _label in stages
        )
        frist = f"{row.deadline_days} T" if row.deadline_days is not None else "?"
        body.append(
            "<tr>"
            f'<td class="num mono">{escape(frist)}</td>'
            f'<td><a href="/tender/{escape(row.tender_id)}">{esc(row.title)}</a></td>'
            f"{marks}"
            f"<td>{_decision_pill(row.decision, stale=row.is_stale)}</td>"
            f'<td class="mono">{escape(row.next_step)}</td>'
            "</tr>"
        )

    return (
        f"<h2>Uebersicht ({len(rows)})</h2>"
        '<p class="lede">Nach Frist sortiert. Ein Haken heisst: diese Stufe ist gelaufen. '
        "Die Entscheidung trifft ein Mensch.</p>"
        '<div class="wrap"><table><thead><tr>'
        '<th class="num">Frist</th><th>Titel</th>'
        f"{head}<th>Entscheidung</th><th>Naechster Schritt</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
    )


def _facts(pairs: list[tuple[str, str]]) -> str:
    items = "".join(f"<dt>{escape(key)}</dt><dd>{value}</dd>" for key, value in pairs)
    return f'<dl class="facts">{items}</dl>'


def detail(
    *,
    tender: Any,
    record: Any,
    risk: Any,
    items_count: int,
    pricing: Any,
    calculation: Any,
    criteria: list[Any],
    decisions: list[Any],
    changes: list[Any],
    blockers: list[str],
    is_stale: bool,
    csrf_token: str,
    decided_by: str,
    message: str | None = None,
) -> str:
    parts: list[str] = [f"<h2>{esc(tender.title)}</h2>"]
    parts.append(
        f'<p class="lede mono">{esc(record.id)} &middot; Quelle {esc(record.source)}'
        + (
            f' &middot; <a href="{escape(record.source_url)}" rel="noreferrer noopener">'
            "zur Bekanntmachung</a>"
            if record.source_url
            else ""
        )
        + "</p>"
    )

    if message:
        parts.append(f'<div class="note">{escape(message)}</div>')

    days = tender.days_until_deadline
    parts.append(
        _facts(
            [
                ("Vergabestelle", esc(record.contracting_authority)),
                (
                    "Frist",
                    f"{esc(record.submission_deadline)}"
                    + (f' <span class="mono">({days} Tage)</span>' if days is not None else ""),
                ),
                ("Volumen", money(record.estimated_value, record.currency)),
                ("Status", esc(record.status)),
                (
                    "Risiko",
                    pending(
                        f"{risk.score} ({risk.level})" if risk else None,
                        hint="noch nicht bewertet",
                    ),
                ),
                (
                    "Positionen",
                    pending(items_count or None, hint="noch nicht erkannt"),
                ),
                (
                    "Preisbild",
                    pending(
                        f"{pricing.priced_items}/{pricing.item_count} bepreist"
                        if pricing
                        else None,
                        hint="noch nicht recherchiert",
                    ),
                ),
            ]
        )
    )

    if calculation is not None:
        parts.append("<h3>Kalkulation</h3>")
        parts.append(
            _facts(
                [
                    ("Urteil", esc(calculation.verdict)),
                    ("Score", esc(calculation.score)),
                    ("Abdeckung", f"{esc(calculation.coverage_percent)} %"),
                    ("Selbstkosten", money(calculation.cost_total, calculation.currency)),
                    ("Angebotspreis", money(calculation.sale_total, calculation.currency)),
                    (
                        "Marge",
                        esc(
                            f"{calculation.margin_percent:.1f} %"
                            if calculation.margin_percent is not None
                            else None
                        ),
                    ),
                ]
            )
        )
    if criteria:
        rows = "".join(
            "<tr>"
            f"<td>{esc(item.get('label'))}</td>"
            f'<td class="mono">{esc(item.get("required"))}</td>'
            f'<td class="mono">{esc(item.get("actual"))}</td>'
            f"<td>{_criterion_pill(item)}</td>"
            "</tr>"
            for item in criteria
        )
        parts.append(
            '<h3>Mindestkriterien</h3><div class="wrap"><table><thead><tr>'
            "<th>Kriterium</th><th>Verlangt</th><th>Ist</th><th>Ergebnis</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )

    parts.append(
        _decision_section(decisions, blockers, is_stale, csrf_token, decided_by, record.id)
    )

    if changes:
        rows = "".join(
            "<tr>"
            f'<td class="mono">{esc(change.detected_at)}</td>'
            f"<td>{esc(change.field)}</td>"
            f"<td>{esc(change.old_value)}</td>"
            f"<td>{esc(change.new_value)}</td>"
            "</tr>"
            for change in changes
        )
        parts.append(
            '<h3>Aenderungen</h3><div class="wrap"><table><thead><tr>'
            "<th>Erkannt</th><th>Feld</th><th>Vorher</th><th>Nachher</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )

    parts.append(
        '<p class="hint">Diese Oberflaeche gibt kein Angebot ab. Nach einer Freigabe '
        "erzeugt <code>tender-ai offer</code> einen Entwurf, den ein Mensch prueft "
        "und selbst einreicht.</p>"
    )
    return "".join(parts)


def _decision_section(
    decisions: list[Any],
    blockers: list[str],
    is_stale: bool,
    csrf_token: str,
    decided_by: str,
    tender_id: str,
) -> str:
    parts = ["<h3>Freigabe</h3>"]
    for blocker in blockers:
        css = "note note-bad" if is_stale else "note"
        parts.append(f'<div class="{css}">{escape(blocker)}</div>')

    if decisions:
        rows = "".join(
            "<tr>"
            f'<td class="mono">{esc(decision.decided_at)}</td>'
            f"<td>{_decision_pill(decision.kind, stale=False)}</td>"
            f"<td>{esc(decision.decided_by)}</td>"
            f"<td>{esc(decision.note, missing='-')}</td>"
            "</tr>"
            for decision in decisions
        )
        parts.append(
            '<div class="wrap"><table><thead><tr>'
            "<th>Wann</th><th>Was</th><th>Wer</th><th>Notiz</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )

    parts.append(
        f'<form class="decide" method="post" action="/tender/{escape(tender_id)}/decide">'
        f'<input type="hidden" name="{CSRF_FIELD}" value="{escape(csrf_token)}">'
        '<label for="decided_by">Wer entscheidet (steht so im Protokoll)</label>'
        f'<input type="text" id="decided_by" name="decided_by" required '
        f'value="{escape(decided_by)}" autocomplete="name">'
        '<label for="note">Notiz (optional)</label>'
        '<textarea id="note" name="note" rows="2"></textarea>'
        '<div class="buttons">'
        '<button type="submit" name="kind" value="APPROVED">Freigeben</button>'
        '<button class="secondary" type="submit" name="kind" value="ON_HOLD">'
        "Zurueckstellen</button>"
        '<button class="secondary" type="submit" name="kind" value="REJECTED">Ablehnen</button>'
        "</div></form>"
    )
    return "".join(parts)


def login(*, error: str | None = None) -> str:
    error_html = f'<div class="note note-bad">{escape(error)}</div>' if error else ""
    return (
        "<h2>Anmeldung</h2>"
        '<p class="lede">Diese Oberflaeche ist ueber das Netz erreichbar und verlangt '
        "deshalb den Zugangstoken aus <code>TENDER_AI_WEB_TOKEN</code>.</p>"
        f"{error_html}"
        '<form class="decide" method="post" action="/login">'
        '<label for="token">Zugangstoken</label>'
        '<input type="text" id="token" name="token" required autocomplete="off">'
        '<div class="buttons"><button type="submit">Anmelden</button></div>'
        "</form>"
    )
