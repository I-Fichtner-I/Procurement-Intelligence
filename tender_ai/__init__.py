"""tender-ai - Procurement Intelligence Agent.

Stufe 1 (implementiert): automatisierte Recherche oeffentlicher Ausschreibungen.
Die weiteren Stufen (Analyse, Artikelextraktion, Preisrecherche, Kalkulation,
Profitabilitaet, Scoring) bauen auf denselben Kernbausteinen auf; siehe
docs/architecture.md.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    #: Eine Wahrheit: die Version steht in pyproject.toml und wird von dort
    #: gelesen. Der Fallback greift nur, wenn das Paket nicht installiert ist
    #: (Ausfuehrung direkt aus dem Quellbaum).
    __version__ = _package_version("tender-ai")
except PackageNotFoundError:  # pragma: no cover - nur ohne Installation
    __version__ = "0.0.0+unbekannt"

__all__ = ["__version__"]
