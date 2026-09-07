"""Offline-Quelle aus einer lokalen JSON-Datei.

Zweck: die gesamte Pipeline laesst sich ohne Netzwerk demonstrieren und
testen. Die Datei enthaelt Beispiel-Ausschreibungen im Standardformat.
Fuer Produktivlaeufe ist diese Quelle in config.yaml deaktiviert.

Damit "ohne Netzwerk" auch fuer Stufe 2 gilt, darf ein Dokument statt einer
URL einen Pfad neben der Fixture-Datei tragen. Er wird kopiert, nicht
abgerufen - und er darf das Verzeichnis der Fixture-Datei nicht verlassen.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ..config import FixtureSourceConfig
from ..core.errors import SourceError
from ..models.tender import DocumentAccess, Tender, TenderDocument, make_tender_id
from .base import SearchQuery, TenderSource, safe_document_path, suffix_for
from .registry import register_source


@register_source
class FixtureSource(TenderSource):
    type_name = "fixture"

    config: FixtureSourceConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.path = Path(self.config.path)

    async def search(self, query: SearchQuery) -> list[Tender]:
        if not self.path.is_file():
            raise SourceError(self.name, f"Fixture-Datei nicht gefunden: {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SourceError(self.name, f"Fixture ist kein gueltiges JSON: {exc}") from exc

        records = payload.get("tenders") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise SourceError(self.name, "Fixture muss eine Liste von Ausschreibungen enthalten")

        results: list[Tender] = []
        for record in records:
            record = dict(record)
            record.setdefault("source", self.name)
            source_id = str(record.get("source_id") or record.get("id") or len(results))
            record["source_id"] = source_id
            record["id"] = make_tender_id(self.name, source_id)
            tender = Tender.model_validate(record)
            if query.matches(tender):
                results.append(tender)
            if len(results) >= query.max_results:
                break
        return results

    async def get_tender_details(self, tender_id: str) -> Tender | None:
        for tender in await self.search(SearchQuery(max_results=10_000)):
            if tender.id == tender_id or tender.source_id == tender_id:
                return tender
        return None

    async def download_documents(self, tender: Tender, destination: Path) -> list[TenderDocument]:
        """Beiliegende Unterlagen kopieren, entfernte weiterhin laden.

        Ein Demolauf soll ohne Netz bis zur Kalkulation kommen; dafuer muessen
        die Unterlagen aus dem Repository stammen duerfen. Alles, was nicht
        neben der Fixture-Datei liegt, geht unveraendert den normalen Weg.
        """
        copied: list[TenderDocument] = []
        remote = Tender(**{**tender.model_dump(), "documents": []})
        for index, document in enumerate(tender.documents):
            if document.access is not DocumentAccess.PUBLIC or not document.url:
                continue
            try:
                local = self._local_source(document.url)
            except ValueError as exc:
                # Abgelehnt heisst abgelehnt: ein Pfad, der aus dem
                # Fixture-Verzeichnis zeigt, wird auch nicht als URL versucht.
                document.note = str(exc)
                self.log.warning("fixture_document_rejected", url=document.url, reason=str(exc))
                continue
            if local is None:
                remote.documents.append(document)
                continue
            if not local.is_file():
                document.note = f"Beiliegende Unterlage fehlt: {local}"
                self.log.warning("fixture_document_missing", path=str(local))
                continue
            suffix = suffix_for(document.media_type, document.url) or local.suffix
            name = f"{tender.source_id}-{index}" if len(tender.documents) > 1 else tender.source_id
            target = safe_document_path(destination, tender.source, name, suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local, target)
            document.local_path = str(target)
            document.retrieved_at = datetime.now(UTC)
            document.size_bytes = target.stat().st_size
            document.checksum_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
            copied.append(document)

        if remote.documents:
            copied.extend(await super().download_documents(remote, destination))
        return copied

    def _local_source(self, url: str) -> Path | None:
        """Pfad zu einer beiliegenden Datei - oder ``None`` bei einer echten URL.

        Erlaubt sind ein relativer Pfad und ``file:``. Beides wird gegen das
        Verzeichnis der Fixture-Datei aufgeloest: eine Fixture darf Demodaten
        mitbringen, aber nicht in beliebige Dateien des Rechners zeigen. Zeigt
        der Pfad hinaus, ist das ein ``ValueError`` - kein Rueckfall auf HTTP.
        """
        split = urlsplit(url)
        if split.scheme in {"http", "https"}:
            return None
        if split.scheme == "file":
            # "file://lv.csv" legt den Namen in netloc ab, "file:///pfad" in path.
            candidate = Path(unquote(split.netloc) + unquote(split.path))
        elif split.scheme:
            return None
        else:
            candidate = Path(unquote(url))

        base = self.path.resolve().parent
        resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(f"Beiliegende Unterlage liegt ausserhalb von {base}: {resolved}")
        return resolved
