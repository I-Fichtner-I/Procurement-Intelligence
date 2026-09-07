"""Lokale Weboberflaeche (Stufe 9) - optional installierbar.

Ohne die Zusatzpakete (``pip install -e ".[web]"``) bleibt der Rest des
Programms unberuehrt; erst der Import hier verlangt FastAPI.
"""

from .app import create_app

__all__ = ["create_app"]
