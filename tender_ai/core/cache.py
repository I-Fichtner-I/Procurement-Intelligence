"""Einfacher Dateicache fuer HTTP-Antworten.

Zweck: wiederholte Laeufe waehrend der Entwicklung und beim Debuggen belasten
die Portale nicht erneut. Der Cache ist bewusst simpel (JSON-Datei je
Request-Fingerprint) und jederzeit loeschbar.

Der Cache raeumt selbst auf: abgelaufene Eintraege werden beim Start des
HTTP-Clients entfernt, und ueberzaehlige Eintraege verdraengt eine LRU-Regel
nach Speicherzeitpunkt. Ohne das waechst das Verzeichnis unbegrenzt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

#: Nach so vielen Schreibvorgaengen wird die Groesse geprueft. Ein
#: Verzeichnis-Scan je ``set`` waere teurer als der Cache einspart.
_PRUNE_INTERVAL = 200


class ResponseCache:
    def __init__(
        self,
        directory: Path,
        ttl_seconds: int = 900,
        enabled: bool = True,
        max_entries: int = 5000,
    ) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled
        self.max_entries = max_entries
        self._writes_since_prune = 0
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(method: str, url: str, body: bytes | None = None, auth: str | None = None) -> str:
        """Fingerprint eines Requests.

        ``auth`` (Wert des ``Authorization``-Headers) geht als Hash mit ein:
        zwei Nutzer mit verschiedenen Schluesseln duerfen sich nie eine
        gecachte Antwort teilen. Der Wert selbst wird nie gespeichert.
        """
        digest = hashlib.sha256()
        digest.update(method.upper().encode())
        digest.update(b"\x00")
        digest.update(url.encode())
        digest.update(b"\x00")
        if body:
            digest.update(body)
        if auth:
            digest.update(b"\x00")
            digest.update(hashlib.sha256(auth.encode()).digest())
        return digest.hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def _entries(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return list(self.directory.glob("*.json"))

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if time.time() - payload.get("stored_at", 0) > self.ttl_seconds:
            return None
        payload["content"] = base64.b64decode(payload["content_b64"])
        return payload

    def set(
        self,
        key: str,
        *,
        status_code: int,
        content: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not self.enabled:
            return
        payload = {
            "stored_at": time.time(),
            "status_code": status_code,
            "headers": headers or {},
            "content_b64": base64.b64encode(content).decode("ascii"),
        }
        tmp = self._path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._path(key))
        self._writes_since_prune += 1
        if self._writes_since_prune >= _PRUNE_INTERVAL:
            self.prune()

    def evict_expired(self) -> int:
        """Abgelaufene Eintraege loeschen; Anzahl der entfernten zurueckgeben."""
        if not self.enabled:
            return 0
        deadline = time.time() - self.ttl_seconds
        removed = 0
        for file in self._entries():
            try:
                if file.stat().st_mtime <= deadline:
                    file.unlink(missing_ok=True)
                    removed += 1
            except OSError:  # parallel geloescht - dann ist das Ziel erreicht
                continue
        return removed

    def prune(self) -> int:
        """Abgelaufene und ueberzaehlige Eintraege entfernen (LRU nach Speicherzeit)."""
        self._writes_since_prune = 0
        removed = self.evict_expired()
        entries: list[tuple[float, Path]] = []
        for file in self._entries():
            try:
                entries.append((file.stat().st_mtime, file))
            except OSError:
                continue
        surplus = len(entries) - self.max_entries
        if surplus <= 0:
            return removed
        entries.sort(key=lambda item: item[0])
        for _mtime, file in entries[:surplus]:
            file.unlink(missing_ok=True)
            removed += 1
        return removed

    def clear(self) -> int:
        removed = 0
        for file in self._entries():
            file.unlink(missing_ok=True)
            removed += 1
        return removed
