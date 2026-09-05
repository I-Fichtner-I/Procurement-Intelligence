"""Rechercheablauf als wiederverwendbarer Dienst."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy.orm import Session

from ..config import Settings
from ..core.errors import ConfigError
from ..core.http import build_http_client
from ..database.session import session_factory
from ..pipeline.ingest import IngestReport, IngestService
from ..sources.base import SearchQuery
from ..sources.registry import build_sources


async def run_search(
    settings: Settings,
    query: SearchQuery,
    *,
    only_sources: Sequence[str] | None = None,
    store: bool = True,
    download_documents: bool = False,
    session: Session | None = None,
) -> IngestReport:
    """Konfigurierte Quellen durchsuchen und die Treffer speichern.

    ``session`` kann uebergeben werden (Tests, laufende Transaktion); sonst
    oeffnet die Persistenz ihre Sessions selbst - je Schreibeinheit eine, in
    einem Thread, damit der Event-Loop waehrenddessen ansprechbar bleibt. Ist
    keine Quelle aktiv oder ausgewaehlt, wird ``ConfigError`` geworfen - die
    Oberflaeche entscheidet, wie sie das meldet.
    """
    http = build_http_client(settings.http, settings.cache_dir)
    try:
        sources = build_sources(settings, http, only=only_sources)
        if not sources:
            raise ConfigError(
                "Keine aktive Quelle gefunden. Pruefe config.yaml "
                "(sources.<name>.enabled) oder die Auswahl per --source."
            )
        if session is not None:
            service = IngestService(settings, sources, http, session=session)
        elif store:
            service = IngestService(
                settings, sources, http, session_factory=session_factory(settings.database_url)
            )
        else:
            service = IngestService(settings, sources, http)
        return await service.run(query, store=store, download_documents=download_documents)
    finally:
        await http.aclose()


def source_names(settings: Settings, only: Iterable[str] | None = None) -> list[str]:
    """Namen der Quellen, die ein Lauf verwenden wuerde - ohne Netzwerkzugriff."""
    wanted = {name.lower() for name in only} if only else None
    return [
        name
        for name, config in sorted(settings.sources.items(), key=lambda item: item[1].priority)
        if (wanted is None and config.enabled) or (wanted is not None and name.lower() in wanted)
    ]
