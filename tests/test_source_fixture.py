from __future__ import annotations

import json
from pathlib import Path

import pytest

from tender_ai.config import Settings
from tender_ai.core.errors import SourceError
from tender_ai.core.http import HttpClient
from tender_ai.sources.base import SearchQuery
from tender_ai.sources.fixture import FixtureSource


def build_source(settings: Settings) -> FixtureSource:
    return FixtureSource(
        name="fixture",
        config=settings.sources["fixture"],
        http=HttpClient(settings.http),
        settings=settings,
    )


async def test_fixture_source_loads_tenders(settings: Settings):
    source = build_source(settings)
    results = await source.search(SearchQuery(max_results=10))
    assert {t.source_id for t in results} == {"t-1", "t-2"}
    assert all(t.id.startswith("fixture:") for t in results)


async def test_fixture_source_applies_filters(settings: Settings):
    source = build_source(settings)
    results = await source.search(SearchQuery(keywords=["Monitor"], max_results=10))
    assert [t.source_id for t in results] == ["t-1"]

    cpv_results = await source.search(SearchQuery(cpv_codes=["50750000"], max_results=10))
    assert [t.source_id for t in cpv_results] == ["t-2"]


async def test_missing_file_raises_source_error(settings: Settings, tmp_path):
    settings.sources["fixture"].path = str(tmp_path / "fehlt.json")
    source = build_source(settings)
    with pytest.raises(SourceError):
        await source.search(SearchQuery())


async def test_get_tender_details(settings: Settings):
    source = build_source(settings)
    tender = await source.get_tender_details("t-1")
    assert tender is not None and tender.title.startswith("Lieferung")
    assert await source.get_tender_details("gibt-es-nicht") is None


async def test_health_check_ok(settings: Settings):
    status = await build_source(settings).health_check()
    assert status.ok is True and status.sample_count == 1


def _with_document(settings: Settings, url: str, *, access: str = "PUBLIC") -> Path:
    """Der ersten Fixture-Ausschreibung ein Dokument mit dieser URL geben."""
    path = Path(settings.sources["fixture"].path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tenders"][0]["documents"] = [
        {"name": "Leistungsverzeichnis", "url": url, "media_type": "text/csv", "access": access}
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def test_beiliegende_unterlage_wird_kopiert(settings: Settings, tmp_path: Path):
    """Eine Datei neben der Fixture darf ohne Netz in die Ablage wandern."""
    fixture_path = _with_document(settings, "lv.csv")
    (fixture_path.parent / "lv.csv").write_text("Pos;Bezeichnung\n1;Monitor\n", encoding="utf-8")

    source = build_source(settings)
    tender = await source.get_tender_details("t-1")
    assert tender is not None

    destination = tmp_path / "documents"
    copied = await source.download_documents(tender, destination)

    assert len(copied) == 1
    stored = Path(copied[0].local_path)
    assert stored.is_file() and stored.is_relative_to(destination.resolve())
    assert stored.read_text(encoding="utf-8").startswith("Pos;Bezeichnung")
    assert copied[0].size_bytes == stored.stat().st_size
    assert copied[0].checksum_sha256


async def test_file_url_wird_akzeptiert(settings: Settings, tmp_path: Path):
    fixture_path = _with_document(settings, "file://lv.csv")
    (fixture_path.parent / "lv.csv").write_text("Pos;Bezeichnung\n1;Monitor\n", encoding="utf-8")

    source = build_source(settings)
    tender = await source.get_tender_details("t-1")
    assert tender is not None
    copied = await source.download_documents(tender, tmp_path / "documents")
    assert len(copied) == 1 and copied[0].local_path


async def test_pfad_ausserhalb_des_fixture_verzeichnisses_wird_abgelehnt(
    settings: Settings, tmp_path: Path
):
    """Eine Fixture darf Demodaten mitbringen, aber nicht /etc/passwd lesen."""
    _with_document(settings, "../../etc/passwd")

    source = build_source(settings)
    tender = await source.get_tender_details("t-1")
    assert tender is not None
    destination = tmp_path / "documents"
    copied = await source.download_documents(tender, destination)
    assert copied == []
    assert not destination.exists() or not list(destination.rglob("*"))
    assert "ausserhalb" in (tender.documents[0].note or "")


async def test_fehlende_beiliegende_datei_wird_vermerkt(settings: Settings, tmp_path: Path):
    _with_document(settings, "gibt-es-nicht.csv")

    source = build_source(settings)
    tender = await source.get_tender_details("t-1")
    assert tender is not None
    copied = await source.download_documents(tender, tmp_path / "documents")
    assert copied == []
    assert "fehlt" in (tender.documents[0].note or "").lower()


async def test_geschuetzte_unterlage_wird_nicht_angefasst(settings: Settings, tmp_path: Path):
    fixture_path = _with_document(settings, "lv.csv", access="REGISTRATION")
    (fixture_path.parent / "lv.csv").write_text("Pos;Bezeichnung\n1;Monitor\n", encoding="utf-8")

    source = build_source(settings)
    tender = await source.get_tender_details("t-1")
    assert tender is not None
    assert await source.download_documents(tender, tmp_path / "documents") == []
    assert tender.documents[0].local_path is None


def test_mitgelieferte_demodaten_sind_vollstaendig():
    """Die Demo im Repository muss ohne Netz bis zu den Positionen kommen.

    Sonst verspricht die Anleitung einen Offline-Lauf, der nach Stufe 2 endet.
    """
    fixture = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "sample_tenders.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    local = [
        document
        for tender in payload["tenders"]
        for document in tender.get("documents", [])
        if document.get("access") == "PUBLIC" and "://" not in document.get("url", "")
    ]
    assert local, "kein beiliegendes Leistungsverzeichnis in den Demodaten"
    for document in local:
        assert (fixture.parent / document["url"]).is_file()
