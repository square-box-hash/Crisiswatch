"""Mongo persistence — the part that makes cases track over time."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    client = mongomock.MongoClient()
    monkeypatch.setattr(db, "outbreaks", client["crisiswatch"]["outbreaks"])
    monkeypatch.setattr(db, "pipeline_runs", client["crisiswatch"]["pipeline_runs"])
    yield
    client.close()


def make_doc(cases=120, deaths=4, disease="Cholera", country="Zambia"):
    return {
        "disease": disease,
        "country": country,
        "region": country,
        "estimated_cases": cases,
        "estimated_deaths": deaths,
        "severity": "moderate",
        "confidence_score": 0.7,
        "source_urls": ["https://example.org/a"],
        "source_names": ["WHO DON"],
        "incident_key": store.incident_key(disease, country),
    }


def test_first_insert_creates_one_document_with_one_snapshot():
    result = store.upsert_incident(make_doc())

    assert result["created"] is True
    assert db.outbreaks.count_documents({}) == 1

    doc = db.outbreaks.find_one({})
    assert doc["incident_key"] == "cholera:zambia"
    assert len(doc["snapshots"]) == 1
    assert doc["first_seen"] is not None
    assert doc["peak_cases"] == 120


def test_second_run_same_day_updates_instead_of_duplicating():
    now = datetime.now(timezone.utc)

    store.upsert_incident(make_doc(cases=120), now=now)
    result = store.upsert_incident(make_doc(cases=150), now=now)

    assert result["created"] is False
    assert result["updated"] is True
    assert result["cases_delta"] == 30
    assert db.outbreaks.count_documents({}) == 1

    doc = db.outbreaks.find_one({})
    assert doc["estimated_cases"] == 150
    assert doc["peak_cases"] == 150
    # Same day → still one snapshot, updated in place.
    assert len(doc["snapshots"]) == 1
    assert doc["snapshots"][0]["cases"] == 150


def test_next_day_appends_a_second_snapshot():
    day_one = datetime.now(timezone.utc) - timedelta(days=1)
    day_two = datetime.now(timezone.utc)

    store.upsert_incident(make_doc(cases=120), now=day_one)
    store.upsert_incident(make_doc(cases=210), now=day_two)

    doc = db.outbreaks.find_one({})
    assert len(doc["snapshots"]) == 2
    assert [s["cases"] for s in doc["snapshots"]] == [120, 210]
    assert doc["peak_cases"] == 210


def test_a_correction_downwards_keeps_the_peak():
    day_one = datetime.now(timezone.utc) - timedelta(days=1)
    day_two = datetime.now(timezone.utc)

    store.upsert_incident(make_doc(cases=500), now=day_one)
    store.upsert_incident(make_doc(cases=410), now=day_two)

    doc = db.outbreaks.find_one({})
    assert doc["estimated_cases"] == 410
    assert doc["peak_cases"] == 500
    assert doc["snapshots"][-1]["cases"] == 410


def test_incident_key_is_case_and_punctuation_insensitive():
    a = store.incident_key("Ebola (Bundibugyo)", "DRC")
    b = store.incident_key("ebola bundibugyo", "drc")
    assert a == b == "ebola-bundibugyo:drc"


def test_different_countries_are_different_outbreaks():
    store.upsert_incident(make_doc(country="Zambia"))
    store.upsert_incident(make_doc(country="Malawi"))
    assert db.outbreaks.count_documents({}) == 2


def test_run_log_records_and_reads_back():
    run_id = store.start_run()
    store.finish_run(
        run_id,
        {"status": "ok", "articles_fetched": 7, "items_extracted": 3},
    )

    latest = store.latest_run()
    assert latest["status"] == "ok"
    assert latest["articles_fetched"] == 7
    assert isinstance(latest["started_at"], str)


def test_missing_mongo_uri_raises_a_clear_error(monkeypatch):
    monkeypatch.setattr(db, "outbreaks", None)

    with pytest.raises(RuntimeError) as excinfo:
        store.upsert_incident(make_doc())

    assert "MONGODB_URI" in str(excinfo.value)


def test_stale_runs_are_closed_out():
    old = datetime.now(timezone.utc) - timedelta(hours=1)

    db.pipeline_runs.insert_one(
        {"status": "running", "started_at": old, "errors": []}
    )
    db.pipeline_runs.insert_one(
        {"status": "running", "started_at": datetime.now(timezone.utc), "errors": []}
    )

    assert store.mark_stale_runs(minutes=15) == 1

    statuses = [d["status"] for d in db.pipeline_runs.find({})]
    assert sorted(statuses) == ["abandoned", "running"]


# ── Evidence, provenance, and never downgrading a figure ────

def ev_doc(cases, source="WHO DON", url="https://who.int/a", tier="HIGH"):
    return {
        "disease": "Ebola (Bundibugyo)",
        "country": "Kenya",
        "estimated_cases": cases,
        "estimated_deaths": None,
        "incident_key": store.incident_key("Ebola (Bundibugyo)", "Kenya"),
        "evidence": [
            {
                "source_name": source,
                "url": url,
                "tier": tier,
                "cases": cases,
                "deaths": None,
                "confidence": 0.9,
                "published_at": None,
                "excerpt": "text",
            }
        ],
    }


def test_a_stated_count_is_stored_with_its_source():
    store.upsert_incident(ev_doc(635))

    doc = db.outbreaks.find_one({})
    assert doc["estimated_cases"] == 635
    assert doc["cases_source"] == "https://who.int/a"
    assert doc["cases_unknown"] is False
    assert doc["status"] == "probable"
    assert len(doc["evidence"]) == 1


def test_an_unknown_later_report_does_not_erase_the_count():
    store.upsert_incident(ev_doc(635))
    store.upsert_incident(
        {
            "disease": "Ebola (Bundibugyo)",
            "country": "Kenya",
            "estimated_cases": None,
            "incident_key": store.incident_key("Ebola (Bundibugyo)", "Kenya"),
            "evidence": [
                {
                    "source_name": "CDC Travel Notices",
                    "url": "https://cdc.gov/n",
                    "tier": "HIGH",
                    "cases": None,
                    "deaths": None,
                    "confidence": 0.8,
                }
            ],
        }
    )

    doc = db.outbreaks.find_one({})
    assert doc["estimated_cases"] == 635, "unknown must not overwrite a known figure"
    assert doc["cases_unknown"] is False
    assert len(doc["evidence"]) == 2


def test_second_source_upgrades_status_to_confirmed():
    store.upsert_incident(ev_doc(635))
    store.upsert_incident(
        {
            "disease": "Ebola (Bundibugyo)",
            "country": "Kenya",
            "incident_key": store.incident_key("Ebola (Bundibugyo)", "Kenya"),
            "evidence": [
                {
                    "source_name": "ReliefWeb",
                    "url": "https://reliefweb.int/r",
                    "tier": "HIGH",
                    "cases": 640,
                    "deaths": 127,
                    "confidence": 0.85,
                }
            ],
        }
    )

    doc = db.outbreaks.find_one({})
    assert doc["status"] == "confirmed"
    assert doc["sources_agreeing"] == 2
    assert doc["estimated_cases"] == 640
    assert doc["estimated_deaths"] == 127


# ── Retention: keep only quantified records ─────────────────

def insert(cases, disease="Cholera", country="Zambia", status="signal"):
    db.outbreaks.insert_one(
        {
            "incident_key": store.incident_key(disease, country),
            "disease": disease,
            "country": country,
            "estimated_cases": cases,
            "status": status,
        }
    )


def test_purge_removes_records_without_a_count():
    insert(None, disease="Typhoid Fever", country="Senegal")
    insert(412, disease="Cholera", country="Zambia")

    result = store.purge_unquantified()

    assert result["deleted"] == 1
    assert result["kept"] == 1
    assert db.outbreaks.find_one({})["disease"] == "Cholera"


def test_purge_removes_explicit_zero():
    insert(0, disease="Polio", country="Afghanistan")
    insert(635, disease="Ebola", country="Kenya")

    assert store.purge_unquantified()["deleted"] == 1
    assert db.outbreaks.count_documents({}) == 1


def test_purge_keeps_a_single_case():
    insert(1, disease="Measles", country="Yemen")

    assert store.purge_unquantified()["deleted"] == 0
    assert db.outbreaks.count_documents({}) == 1


def test_purge_on_an_empty_collection_is_harmless():
    result = store.purge_unquantified()

    assert result == {"deleted": 0, "kept": 0, "sample": []}


def test_purge_reports_what_it_removed():
    insert(None, disease="Typhoid Fever", country="Senegal")
    insert(None, disease="Malaria", country="Panama")

    result = store.purge_unquantified()

    assert result["deleted"] == 2
    assert "Typhoid Fever/Senegal" in result["sample"]
    assert result["sample_total"] == 2
