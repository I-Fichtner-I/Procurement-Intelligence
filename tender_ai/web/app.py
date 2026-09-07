"""Die Weboberflaeche: sehen, was ansteht - und freigeben (Stufe 9).

Bewusst klein gehalten. Drei Ansichten (Uebersicht, Detail, Anmeldung) und
genau **ein** schreibender Endpunkt: die Entscheidung eines Menschen. Alles
andere - recherchieren, analysieren, kalkulieren - bleibt beim Takt und der
Kommandozeile; eine Oberflaeche, die nebenbei Portale abfragt, waere ein
zweiter Ort, an dem dieselbe Logik gepflegt werden muesste.

Die Schutzregeln stehen in ``security.py`` und gelten fuer jede Anfrage.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import __version__
from ..config import Settings
from ..core.errors import ConfigError
from ..core.logging import get_logger
from ..database.repository import TenderRepository
from ..database.session import session_scope
from ..models.decision import DecisionKind
from ..services.approval import approval_state, pipeline_status, record_decision
from . import render
from .security import (
    CSRF_COOKIE,
    CSRF_FIELD,
    TOKEN_COOKIE,
    is_loopback,
    new_csrf_token,
    token_from_request,
    tokens_match,
)

log = get_logger(__name__)

#: Pfade, die ohne Anmeldung erreichbar sein muessen.
PUBLIC_PATHS = frozenset({"/login", "/health"})


def create_app(settings: Settings) -> FastAPI:
    """Die Anwendung bauen - und den Start verweigern, wenn sie ungeschuetzt offen stuende."""
    token = settings.web_token.get_secret_value() if settings.web_token else None
    if not token and not is_loopback(settings.web.host):
        raise ConfigError(
            f"web.host ist '{settings.web.host}', also nicht nur der eigene Rechner. "
            "Dann ist ein Zugangstoken Pflicht: TENDER_AI_WEB_TOKEN setzen (und die "
            "Oberflaeche hinter TLS betreiben) oder host auf 127.0.0.1 lassen."
        )

    app = FastAPI(title=settings.web.title, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.token = token

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        """Zugang pruefen und den CSRF-Wert setzen - vor jeder Ansicht."""
        if request.url.path not in PUBLIC_PATHS:
            if token:
                presented = token_from_request(
                    dict(request.cookies), request.headers.get("Authorization")
                )
                if not tokens_match(token, presented):
                    return RedirectResponse("/login", status_code=303)
            elif not is_loopback(request.client.host if request.client else None):
                # Ohne Token bleibt die Oberflaeche auf den eigenen Rechner beschraenkt,
                # auch wenn jemand einen Reverse Proxy davorstellt.
                log.warning(
                    "web_remote_access_denied",
                    client=getattr(request.client, "host", None),
                )
                return HTMLResponse(
                    render.page(
                        settings.web.title,
                        '<h2>Kein Zugriff</h2><p class="lede">Ohne Zugangstoken ist diese '
                        "Oberflaeche nur auf dem Rechner erreichbar, auf dem sie laeuft.</p>",
                    ),
                    status_code=403,
                )

        csrf = request.cookies.get(CSRF_COOKIE) or new_csrf_token()
        request.state.csrf_token = csrf
        response = await call_next(request)
        if request.cookies.get(CSRF_COOKIE) != csrf:
            response.set_cookie(CSRF_COOKIE, csrf, httponly=True, samesite="strict", path="/")
        return response

    _register_routes(app, settings)
    return app


def _register_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/health")
    async def health() -> JSONResponse:
        with session_scope(settings.database_url) as session:
            count = TenderRepository(session, settings.dedup).count(only_primary=False)
        return JSONResponse({"status": "ok", "version": __version__, "tenders": count})

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> HTMLResponse:
        if not app.state.token:
            return HTMLResponse(status_code=303, content="", headers={"Location": "/"})
        return HTMLResponse(render.page(settings.web.title, render.login()))

    @app.post("/login")
    async def login_submit(request: Request, token: str = Form(...)) -> Response:
        if not tokens_match(app.state.token, token):
            log.warning("web_login_failed")
            return HTMLResponse(
                render.page(settings.web.title, render.login(error="Token stimmt nicht.")),
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request, all: bool = False) -> HTMLResponse:
        rows = pipeline_status(settings, limit=200, open_only=not all)
        return HTMLResponse(
            render.page(
                settings.web.title,
                render.overview(rows, open_only=not all),
                subtitle=f"v{__version__}",
            )
        )

    @app.get("/tender/{tender_id}", response_class=HTMLResponse)
    async def detail(request: Request, tender_id: str, msg: str | None = None) -> HTMLResponse:
        view = _tender_view(settings, tender_id)
        if view is None:
            return HTMLResponse(
                render.page(
                    settings.web.title,
                    f'<h2>Nicht gefunden</h2><p class="lede">{render.esc(tender_id)}</p>',
                ),
                status_code=404,
            )
        return HTMLResponse(
            render.page(
                settings.web.title,
                render.detail(
                    csrf_token=request.state.csrf_token,
                    decided_by=settings.web.decided_by,
                    message=msg,
                    **view,
                ),
                subtitle=tender_id,
            )
        )

    @app.post("/tender/{tender_id}/decide")
    async def decide(
        request: Request,
        tender_id: str,
        kind: str = Form(...),
        decided_by: str = Form(...),
        note: str = Form(""),
        csrf_token: str = Form(..., alias=CSRF_FIELD),
    ) -> Response:
        if not tokens_match(request.cookies.get(CSRF_COOKIE), csrf_token):
            # Ohne diese Pruefung koennte eine fremde Seite im selben Browser
            # eine Freigabe ausloesen.
            log.warning("web_csrf_rejected", tender=tender_id)
            return HTMLResponse(
                render.page(
                    settings.web.title,
                    '<h2>Abgelehnt</h2><p class="lede">Das Formular war nicht mehr gueltig. '
                    "Bitte die Seite neu laden und erneut entscheiden.</p>",
                ),
                status_code=400,
            )

        try:
            decision_kind = DecisionKind(kind)
        except ValueError:
            return RedirectResponse(
                f"/tender/{tender_id}?msg=Unbekannte+Entscheidung", status_code=303
            )

        try:
            record_decision(
                settings,
                tender_id,
                decision_kind,
                decided_by=decided_by.strip() or "unbekannt",
                note=note.strip() or None,
            )
        except ConfigError as exc:
            return RedirectResponse(f"/tender/{tender_id}?msg={exc}", status_code=303)

        log.info("web_decision", tender=tender_id, kind=str(decision_kind), by=decided_by)
        return RedirectResponse(
            f"/tender/{tender_id}?msg=Entscheidung+protokolliert", status_code=303
        )


def _tender_view(settings: Settings, tender_id: str) -> dict[str, Any] | None:
    """Alles, was die Detailansicht zeigt - in einer Sitzung gelesen."""
    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, settings.dedup)
        record = repository.get(tender_id)
        if record is None:
            return None

        calculation = record.calculation
        view: dict[str, Any] = {
            "tender": TenderRepository.to_tender(record, with_raw=False),
            "record": record,
            "risk": record.risk_analysis,
            "items_count": len(record.items),
            "pricing": record.price_research,
            "calculation": calculation,
            "criteria": list(calculation.criteria or []) if calculation else [],
            "decisions": repository.decisions_for(record.id, limit=10),
            "changes": repository.changes_for(record.id, limit=15),
        }

    state = approval_state(settings, tender_id)
    view["blockers"] = list(state.blockers)
    view["is_stale"] = state.is_stale
    return view
