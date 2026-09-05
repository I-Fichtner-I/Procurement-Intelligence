"""T-27: Wirkung der ausgelagerten Rohdaten auf die haeufig gelesene Zeile.

Die Datei wird als Ganzes nicht kleiner - dieselben Daten liegen nur woanders.
Kleiner wird, was jede Liste, jeder Export und jede Dublettenpruefung mitliest:
``tenders.payload``. Genau das wird hier gemessen.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from tender_ai.config import DedupConfig
from tender_ai.database.repository import TenderRepository
from tender_ai.database.session import session_scope
from tender_ai.models.tender import Tender

#: Rohantwort einer Bekanntmachung ist ein Vielfaches der normalisierten
#: Felder. Weniger als die Haelfte Ersparnis hiesse, dass ``raw`` wieder im
#: payload landet.
MIN_SAVING_PERCENT = 50.0
COUNT = 200


def _notice(index: int) -> dict:
    return {
        "publication-number": f"{index:08d}-2026",
        "notice-title": {"deu": [f"Lieferung von {index} Monitoren"]},
        "description-lot": {"deu": ["Beschaffung fuer Verwaltungsarbeitsplaetze. " * 20]},
        "extra": {f"feld-{n}": f"wert-{n}" for n in range(40)},
    }


def _tender(index: int) -> Tender:
    return Tender(
        id=f"ted:{index}",
        source="ted",
        source_id=str(index),
        title=f"Lieferung von {index} Monitoren",
        contracting_authority=f"Musterstadt {index % 50}",
        country="DEU",
        cpv_codes=["30231300"],
        raw=_notice(index),
    )


@pytest.mark.slow
def test_payload_shrinks_when_raw_moves_to_its_own_table(settings):
    with session_scope(settings.database_url) as session:
        repository = TenderRepository(session, DedupConfig(enabled=False))
        for index in range(COUNT):
            repository.upsert(_tender(index))
        session.commit()

        ohne_raw = session.execute(text("SELECT SUM(LENGTH(payload)) FROM tenders")).scalar()
        raw_bytes = session.execute(text("SELECT SUM(LENGTH(raw)) FROM tender_raw")).scalar()
        # Der frueherere Zustand: raw steckte im payload.
        mit_raw = ohne_raw + raw_bytes

        saving = (mit_raw - ohne_raw) / mit_raw * 100
        assert saving >= MIN_SAVING_PERCENT, (
            f"payload nur {saving:.0f} % kleiner - liegen die Rohdaten wieder darin?"
        )

        # Kein Datenverlust: die Rohdaten sind vollstaendig da.
        stored = session.execute(
            text("SELECT raw FROM tender_raw WHERE tender_id = 'ted:7'")
        ).scalar()
        raw = json.loads(stored) if isinstance(stored, str) else stored
        assert raw == _notice(7)
