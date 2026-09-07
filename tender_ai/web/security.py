"""Zugang und Schreibschutz der Weboberflaeche.

Das Architektur-Review haelt fest (F-30): sobald es einen Endpunkt gibt, der
schreibt, muessen Auth, CSRF-Schutz und Nachvollziehbarkeit **mit dem ersten
Endpunkt** kommen, nicht spaeter. Diese Datei ist die Umsetzung davon.

Drei Regeln:

1. **Ohne Token nur lokal.** Bindet der Server auf etwas anderes als
   127.0.0.1/::1, ist ein Zugangstoken Pflicht - sonst startet er nicht.
   Zusaetzlich weist er zur Laufzeit jede Anfrage ab, die nicht von der
   Loopback-Adresse kommt, solange kein Token gesetzt ist.
2. **Token per Formular oder Kopfzeile.** Verglichen wird in konstanter Zeit.
3. **Schreiben nur mit CSRF-Token.** Doppelte Vorlage: ein zufaelliger Wert im
   Cookie muss mit dem versteckten Feld des Formulars uebereinstimmen. Ohne
   das koennte eine fremde Seite im selben Browser eine Freigabe ausloesen.

Der Token schuetzt nicht die Uebertragung: wer die Oberflaeche ueber ein Netz
erreichbar macht, gehoert hinter TLS (Reverse Proxy). Das steht so auch im
README.
"""

from __future__ import annotations

import secrets

#: Adressen, bei denen "nur der eigene Rechner" gilt.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})

TOKEN_COOKIE = "tender_ai_token"
CSRF_COOKIE = "tender_ai_csrf"
CSRF_FIELD = "csrf_token"


def is_loopback(host: str | None) -> bool:
    """Kommt die Anfrage vom selben Rechner?"""
    if not host:
        return False
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(expected: str | None, presented: str | None) -> bool:
    """Zeitkonstanter Vergleich; fehlende Werte gelten nie als Treffer."""
    if not expected or not presented:
        return False
    return secrets.compare_digest(expected, presented)


def token_from_request(cookies: dict[str, str], authorization: str | None) -> str | None:
    """Zugangstoken aus Cookie oder ``Authorization: Bearer <token>``."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return cookies.get(TOKEN_COOKIE)
