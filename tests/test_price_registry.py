"""Registry der Preisquellen: Auswahl, Abschaltung, unbekannte Typen."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tender_ai.config import Settings, load_settings
from tender_ai.core.http import HttpClient
from tender_ai.pricing.sources import available_price_source_types, build_price_sources


@pytest.fixture
def http(settings: Settings) -> HttpClient:
    return HttpClient(settings.http)


def _configure(settings: Settings, price_sources: dict) -> Settings:
    config = yaml.safe_load(settings.config_file.read_text(encoding="utf-8"))
    config["price_sources"] = price_sources
    settings.config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    return load_settings(settings.config_file)


def test_catalog_type_is_registered():
    assert "catalog" in available_price_source_types()


def test_disabled_source_is_skipped_but_selectable(
    settings: Settings, http: HttpClient, tmp_path: Path
):
    """``--source`` aktiviert eine abgeschaltete Quelle fuer einen Lauf."""
    path = tmp_path / "p.csv"
    path.write_text("Bezeichnung;Preis\nX;1,00\n", encoding="utf-8")
    configured = _configure(
        settings, {"aus": {"type": "catalog", "path": str(path), "enabled": False}}
    )

    assert build_price_sources(configured, http) == []
    assert [s.name for s in build_price_sources(configured, http, only=["aus"])] == ["aus"]


def test_unknown_type_does_not_break_the_run(settings: Settings, http: HttpClient):
    """Ein unbekannter Typ wird protokolliert, nicht geworfen."""
    configured = _configure(settings, {"exotisch": {"type": "gibt-es-nicht"}})
    assert build_price_sources(configured, http) == []


def test_sources_are_ordered_by_priority(settings: Settings, http: HttpClient, tmp_path: Path):
    path = tmp_path / "p.csv"
    path.write_text("Bezeichnung;Preis\nX;1,00\n", encoding="utf-8")
    configured = _configure(
        settings,
        {
            "zweite": {"type": "catalog", "path": str(path), "priority": 20},
            "erste": {"type": "catalog", "path": str(path), "priority": 10},
        },
    )
    assert [s.name for s in build_price_sources(configured, http)] == ["erste", "zweite"]


async def test_health_check_reports_a_usable_list(settings: Settings, tmp_path: Path):
    """Der Gesundheitsbericht zaehlt, was von der Liste kalkulationsfaehig ist."""
    from tender_ai.services.health import check_price_sources

    path = tmp_path / "preise.csv"
    path.write_text(
        "Bezeichnung;Preis;Preisbasis;Waehrung\nMonitor;189,00;netto;EUR\n",
        encoding="utf-8",
    )
    configured = _configure(settings, {"liste": {"type": "catalog", "path": str(path)}})

    statuses = await check_price_sources(configured)
    assert len(statuses) == 1
    status = statuses[0]
    assert status.ok is True
    assert status.kind == "price"
    assert status.sample_count == 1
    assert "1 mit Preis" in status.message


async def test_health_check_names_the_column_that_does_not_exist(
    settings: Settings, tmp_path: Path
):
    """Der haeufigste Einrichtungsfehler - und der stummste - bekommt einen Namen."""
    from tender_ai.services.health import check_price_sources

    path = tmp_path / "preise.csv"
    path.write_text("Bezeichnung;Nettopreis\nMonitor;189,00\n", encoding="utf-8")
    configured = _configure(
        settings,
        {"liste": {"type": "catalog", "path": str(path), "columns": {"amount": "Preis"}}},
    )

    status = (await check_price_sources(configured))[0]
    assert status.ok is False
    assert "columns.amount" in status.message
    # Die vorhandenen Ueberschriften stehen dabei, damit niemand raten muss.
    assert "Nettopreis" in status.message


async def test_health_check_separates_missing_column_from_unreadable_values(
    settings: Settings, tmp_path: Path
):
    """Spalte da, Werte unlesbar: das ist ein anderer Fehler als eine fehlende Spalte."""
    from tender_ai.services.health import check_price_sources

    path = tmp_path / "preise.csv"
    path.write_text("Bezeichnung;Preis\nMonitor;auf Anfrage\n", encoding="utf-8")
    configured = _configure(settings, {"liste": {"type": "catalog", "path": str(path)}})

    status = (await check_price_sources(configured))[0]
    assert status.ok is False
    assert "vorhanden, aber kein Wert" in status.message


async def test_health_check_reports_a_missing_file(settings: Settings, tmp_path: Path):
    from tender_ai.services.health import check_price_sources

    configured = _configure(
        settings, {"liste": {"type": "catalog", "path": str(tmp_path / "fehlt.csv")}}
    )

    status = (await check_price_sources(configured))[0]
    assert status.ok is False
    assert "nicht gefunden" in status.message


async def test_health_check_covers_disabled_sources(settings: Settings, tmp_path: Path):
    """Wer eine Liste einrichtet, will sie pruefen koennen, bevor er sie scharf schaltet."""
    from tender_ai.services.health import check_price_sources

    path = tmp_path / "preise.csv"
    path.write_text("Bezeichnung;Preis;Preisbasis\nMonitor;189,00;netto\n", encoding="utf-8")
    configured = _configure(
        settings, {"aus": {"type": "catalog", "path": str(path), "enabled": False}}
    )

    statuses = await check_price_sources(configured)
    assert [status.name for status in statuses] == ["aus"]
    assert statuses[0].ok is True


def test_named_columns_extend_the_defaults_instead_of_replacing_them(
    settings: Settings, tmp_path: Path
):
    """ "Nur abweichende Spalten muessen genannt werden" - und zwar wirklich.

    Wer eine einzige Spalte umbenannte, verlor vorher alle uebrigen
    Zuordnungen; sichtbar wurde das erst an einer Kalkulation ohne Preise.
    """
    path = tmp_path / "preise.csv"
    path.write_text("Bezeichnung;Nettopreis;Preisbasis\nMonitor;189,00;netto\n", encoding="utf-8")
    configured = _configure(
        settings,
        {"liste": {"type": "catalog", "path": str(path), "columns": {"amount": "Nettopreis"}}},
    )

    columns = configured.price_sources["liste"].columns
    assert columns["amount"] == "Nettopreis"  # genannt: gilt
    assert columns["product_name"] == "Bezeichnung"  # nicht genannt: bleibt


async def test_an_empty_column_name_removes_the_mapping(settings: Settings, tmp_path: Path):
    """Eine Zuordnung muss sich auch aufheben lassen, nicht nur aendern."""
    from tender_ai.services.health import check_price_sources

    path = tmp_path / "preise.csv"
    path.write_text("Bezeichnung;Preis\nMonitor;189,00\n", encoding="utf-8")
    configured = _configure(
        settings,
        {"liste": {"type": "catalog", "path": str(path), "columns": {"amount": ""}}},
    )

    status = (await check_price_sources(configured))[0]
    assert status.ok is False
    assert "keine Spalte zugeordnet (columns.amount)" in status.message
