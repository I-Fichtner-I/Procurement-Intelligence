"""Health-Checks der Quellen als wiederverwendbarer Dienst.

Geprueft werden beide Arten von Quellen: die Ausschreibungsquellen (Stufe 1)
und die Preisquellen (Stufe 4). Eine falsch eingerichtete Preisliste faellt
sonst erst auf, wenn die Kalkulation stumm ohne Zahlen bleibt - der falsche
Zeitpunkt, um von einem falschen Pfad oder einer nicht erkannten Spalte zu
erfahren.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

from ..config import Settings
from ..core.http import build_http_client
from ..pricing.sources.base import PriceSource, PriceSourceStatus
from ..pricing.sources.registry import build_price_sources
from ..sources.base import SourceStatus
from ..sources.registry import build_sources


async def check_sources(
    settings: Settings, only: Sequence[str] | None = None
) -> list[SourceStatus]:
    """Jede konfigurierte Quelle mit einem Probeabruf pruefen.

    Auch deaktivierte Quellen werden geprueft - der Befehl soll zeigen, ob eine
    Quelle funktionieren *wuerde*. Eine leere Liste bedeutet: nichts
    konfiguriert oder nichts ausgewaehlt.
    """
    http = build_http_client(settings.http, settings.cache_dir)
    try:
        sources = build_sources(settings, http, only=only, include_disabled=True)
        if not sources:
            return []
        return list(await asyncio.gather(*(source.health_check() for source in sources)))
    finally:
        await http.aclose()


async def check_price_sources(
    settings: Settings, only: Sequence[str] | None = None
) -> list[PriceSourceStatus]:
    """Jede konfigurierte Preisquelle einmal lesen und melden, was ankommt.

    Wie bei den Ausschreibungsquellen werden auch deaktivierte geprueft: wer
    eine Liste einrichtet, will wissen, ob sie taugt, *bevor* er sie scharf
    schaltet.
    """
    http = build_http_client(settings.http, settings.cache_dir)
    try:
        sources = build_price_sources(settings, http, only=only, include_disabled=True)
        if not sources:
            return []
        return list(await asyncio.gather(*(_timed(source) for source in sources)))
    finally:
        await http.aclose()


async def _timed(source: PriceSource) -> PriceSourceStatus:
    """Dauer mitmessen - die Preisquellen tun das nicht selbst.

    Bei einer grossen Liste ist die Lesezeit die eigentliche Auskunft: eine
    Datei, die zehn Sekunden braucht, kostet diese Zeit bei jeder Position.
    """
    started = time.perf_counter()
    status = await source.health_check()
    status.duration_seconds = time.perf_counter() - started
    return status
