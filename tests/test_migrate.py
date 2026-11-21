"""Legacy v4 documents must survive the upgrade and become visible again."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
from migrate import migrate  # noqa: E402


@pytest.fixture
def collection():
    return mongomock.MongoClient()["crisiswatch"]["outbreaks"]


def legacy(cases, days_ago, disease="Cholera", country="Zambia", extra=None):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    doc = {
        "disease": disease,
        "country": country,
        "region": country,
        "severity": "moderate",
        "cluster_signal": {"cases": cases, "deaths": 3},
        "created_at": when,
        "updated_at": when,
    }
    doc.update(extra or {})
    return doc


def test_legacy_doc_gets_key_and_last_seen(collection):
    collection.insert_one(legacy(120, 1))

    result = migrate(collection)
    doc = collection.find_one({})

    assert result["updated"] == 1
    assert doc["incident_key"] == "cholera:zambia"
    assert doc["last_seen"] is not None
    assert doc["estimated_cases"] == 120
    assert doc["peak_cases"] == 120
    assert len(doc["snapshots"]) == 1


def test_duplicate_legacy_rows_merge_into_one(collection):
    collection.insert_one(legacy(100, 5))
    collection.insert_one(legacy(150, 2))
    collection.insert_one(legacy(180, 1))

    result = migrate(collection)

    assert collection.count_documents({}) == 1
    assert result["deleted"] == 2

    doc = collection.find_one({})
    assert doc["peak_cases"] == 180
    # three distinct days → three snapshots, oldest first
    assert len(doc["snapshots"]) == 3
    assert doc["snapshots"][0]["cases"] == 100
    assert doc["snapshots"][-1]["cases"] == 180


def test_migration_is_idempotent(collection):
    collection.insert_one(legacy(120, 1))
    migrate(collection)
    migrate(collection)

    assert collection.count_documents({}) == 1
    assert len(collection.find_one({})["snapshots"]) == 1


def test_migrated_docs_pass_the_api_days_filter(collection, monkeypatch):
    from datetime import timedelta as td

    collection.insert_one(legacy(120, 1))
    migrate(collection)

    cutoff = datetime.now(timezone.utc) - td(days=7)
    visible = list(collection.find({"last_seen": {"$gte": cutoff}}))
    assert len(visible) == 1
