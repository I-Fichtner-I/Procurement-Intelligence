from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tender_ai.config import Settings
from tender_ai.database.repository import TenderRepository
from tender_ai.database.session import session_scope
from tender_ai.models.tender import Tender, TenderDocument, TenderStatus


def tender(**overrides) -> Tender:
    base = dict(
        id="ted:1",
        source="ted",
        source_id="1",
        title="Lieferung von 2.000 Monitoren",
        contracting_authority="Musterstadt - Zentrale Vergabestelle",
        country="DEU",
        publication_date=date(2026, 8, 25),
        submission_deadline=datetime(2036, 9, 15, 10, tzinfo=UTC),
        estimated_value=420000.0,
        currency="EUR",
        status=TenderStatus.OPEN,
    )
    base.update(overrides)
    return Tender(**base)


@pytest.fixture
def repository(settings: Settings):
    with session_scope(settings.database_url) as session:
        yield TenderRepository(
            session,
            settings.dedup,
            source_priority={name: cfg.priority for name, cfg in settings.sources.items()},
        )


def test_new_tender_is_stored(repository: TenderRepository):
    result = repository.upsert(tender())
    assert result.action == "new"
    assert result.record.is_primary is True
    assert repository.count() == 1
    assert repository.get("ted:1") is not None
    assert repository.get("1") is not None  # Kurzform


def test_unchanged_tender_is_recognised(repository: TenderRepository):
    repository.upsert(tender())
    result = repository.upsert(tender())
    assert result.action == "unchanged"
    assert repository.count() == 1


def test_changed_deadline_is_logged(repository: TenderRepository):
    repository.upsert(tender())
    result = repository.upsert(tender(submission_deadline=datetime(2036, 10, 1, 10, tzinfo=UTC)))
    assert result.action == "updated"
    changed_fields = {change[0] for change in result.changes}
    assert "submission_deadline" in changed_fields
    changes = repository.recent_changes()
    assert any(change.field == "submission_deadline" for change in changes)


def test_new_documents_are_tracked_as_change(repository: TenderRepository):
    repository.upsert(tender())
    result = repository.upsert(
        tender(documents=[TenderDocument(name="LV", url="https://x.invalid/lv.pdf")])
    )
    assert result.action == "updated"
    assert any(change[0] == "documents" for change in result.changes)
    assert len(repository.get("ted:1").documents) == 1


def test_duplicate_from_other_source_is_linked(repository: TenderRepository):
    repository.upsert(tender(national_id="VG-2026-1"))
    result = repository.upsert(
        tender(
            id="feed:9",
            source="feed",
            source_id="9",
            national_id="VG-2026-1",
            title="Anderer Titel derselben Vergabe",
        )
    )
    assert result.action == "duplicate"
    assert result.duplicate_of == "ted:1"
    assert result.duplicate_reason == "national_id"
    assert result.record.is_primary is False
    assert repository.count(only_primary=True) == 1
    assert repository.count(only_primary=False) == 2
    aliases = repository.aliases_for("ted:1")
    assert [(a.source, a.match_confidence) for a in aliases] == [("feed", 100)]


def test_duplicate_detected_by_similar_title(repository: TenderRepository):
    repository.upsert(tender())
    result = repository.upsert(
        tender(
            id="feed:9",
            source="feed",
            source_id="9",
            title="Lieferung von 2.000 Monitoren",  # identischer Titel + Vergabestelle
            submission_deadline=datetime(2036, 9, 15, 10, tzinfo=UTC),
        )
    )
    assert result.action == "duplicate"
    assert result.duplicate_reason in {"fingerprint", "title_similarity"}


def test_higher_priority_source_becomes_primary(repository: TenderRepository):
    # "feed" (Prioritaet 20) zuerst, dann "ted" (Prioritaet 10) -> ted wird primaer
    repository.upsert(tender(id="feed:9", source="feed", source_id="9", national_id="VG-2026-1"))
    result = repository.upsert(tender(national_id="VG-2026-1"))
    assert result.action == "duplicate"
    assert result.record.is_primary is True
    assert repository.get("feed:9").is_primary is False
    assert repository.get("feed:9").primary_tender_id == "ted:1"
    assert [a.source for a in repository.aliases_for("ted:1")] == ["feed"]


def test_different_tenders_are_not_merged(repository: TenderRepository):
    repository.upsert(tender())
    result = repository.upsert(
        tender(
            id="feed:9",
            source="feed",
            source_id="9",
            title="Reinigungsdienstleistungen fuer Schulen",
            contracting_authority="Landkreis Beispiel",
        )
    )
    assert result.action == "new"
    assert repository.count(only_primary=True) == 2


def test_list_filters(repository: TenderRepository):
    repository.upsert(tender())
    repository.upsert(
        tender(
            id="feed:2",
            source="feed",
            source_id="2",
            title="Wartung von Aufzugsanlagen",
            contracting_authority="Stadtwerke",
            submission_deadline=datetime(2020, 1, 1, tzinfo=UTC),
        )
    )
    assert len(repository.list_tenders(open_only=True)) == 1
    assert len(repository.list_tenders(sources=["feed"])) == 1
    assert len(repository.list_tenders(search="monitoren")) == 1
    assert repository.stats()["tenders_primary"] == 2


def test_round_trip_to_pydantic(repository: TenderRepository):
    repository.upsert(tender(cpv_codes=["30231300"]))
    record = repository.get("ted:1")
    restored = TenderRepository.to_tender(record)
    assert restored.title == "Lieferung von 2.000 Monitoren"
    assert restored.cpv_codes == ["30231300"]
    assert restored.estimated_value == 420000.0


def test_source_state_tracking(repository: TenderRepository):
    repository.update_source_state("ted", "ted", success=False, error="HTTP 500")
    repository.update_source_state("ted", "ted", success=False, error="HTTP 500")
    state = {s.name: s for s in repository.source_states()}["ted"]
    assert state.consecutive_failures == 2
    repository.update_source_state("ted", "ted", success=True, result_count=7)
    state = {s.name: s for s in repository.source_states()}["ted"]
    assert state.consecutive_failures == 0
    assert state.last_error is None
    assert state.last_result_count == 7


def test_changes_for_returns_only_that_tender(repository: TenderRepository):
    """T-09: Aenderungen werden per Query geholt, nicht aus den letzten N gefiltert."""
    repository.upsert(tender())
    repository.upsert(tender(id="feed:2", source="feed", source_id="2", title="Anderer Auftrag"))

    # Viele Aenderungen an A, damit ein Fenster von 200 den Eintrag zu B verdraengen wuerde
    for day in range(1, 205):
        repository.upsert(
            tender(submission_deadline=datetime(2036, 9, 15, 10, tzinfo=UTC) + timedelta(days=day))
        )
    repository.upsert(
        tender(
            id="feed:2",
            source="feed",
            source_id="2",
            title="Anderer Auftrag - geaendert",
        )
    )

    changes_b = repository.changes_for("feed:2")
    assert changes_b and all(c.tender_id == "feed:2" for c in changes_b)
    assert any(c.field == "title" for c in changes_b)
    assert len(repository.changes_for("ted:1", limit=10)) == 10


# --- T-27: Rohdaten liegen neben dem Datensatz, nicht im payload ---------------
def test_raw_is_stored_beside_the_payload(repository: TenderRepository):
    repository.upsert(tender(raw={"notice": {"publication-number": "00123456-2026"}}))
    record = repository.get("ted:1")

    assert "raw" not in record.payload
    assert record.raw_record is not None
    assert record.raw_record.raw == {"notice": {"publication-number": "00123456-2026"}}

    # Round-Trip: das Modell bekommt seine Rohdaten wieder.
    restored = TenderRepository.to_tender(record)
    assert restored.raw == {"notice": {"publication-number": "00123456-2026"}}
    # ... und ohne sie, wenn der Aufrufer sie nicht braucht.
    assert TenderRepository.to_tender(record, with_raw=False).raw == {}


def test_raw_follows_the_update(repository: TenderRepository):
    repository.upsert(tender(raw={"stand": "alt"}))
    repository.upsert(tender(raw={"stand": "neu"}, title="Lieferung von 3.000 Monitoren"))
    record = repository.get("ted:1")
    assert record.raw_record.raw == {"stand": "neu"}

    # Liefert die Quelle keine Rohdaten mehr, bleibt kein veralteter Satz stehen.
    repository.upsert(tender(raw={}, title="Lieferung von 4.000 Monitoren"))
    repository.session.flush()
    assert repository.get("ted:1").raw_record is None


def test_raw_is_deleted_with_its_tender(repository: TenderRepository):
    repository.upsert(tender(raw={"gross": "x" * 100}))
    record = repository.get("ted:1")
    repository.session.delete(record)
    repository.session.flush()

    from tender_ai.database.models import TenderRawRecord

    assert repository.session.get(TenderRawRecord, "ted:1") is None


def test_list_can_load_raw_without_extra_queries_per_record(repository: TenderRepository):
    for index in range(3):
        repository.upsert(tender(id=f"ted:{index}", source_id=str(index), raw={"index": index}))
    repository.session.commit()
    repository.session.expunge_all()

    records = repository.list_tenders(include_raw=True)
    assert {record.raw_record.raw["index"] for record in records} == {0, 1, 2}
