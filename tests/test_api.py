"""API contract — the endpoints the dashboard actually calls."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
import db  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    mongo = mongomock.MongoClient()
    database = mongo["crisiswatch"]

    monkeypatch.setattr(db, "outbreaks", database["outbreaks"])
    monkeypatch.setattr(db, "pipeline_runs", database["pipeline_runs"])
    monkeypatch.setattr(db, "health_workers", database["health_workers"])
    monkeypatch.setenv("CRON_SECRET", "test-secret")

    return TestClient(api.app)


def seed(cases=100, deaths=2, severity="moderate", disease="Cholera",
         country="Zambia", days_ago=1, snapshots=None):
    now = datetime.now(timezone.utc)
    db.outbreaks.insert_one(
        {
            "incident_key": f"{disease.lower()}:{country.lower()}",
            "disease": disease,
            "country": country,
            "region": country,
            "estimated_cases": cases,
            "estimated_deaths": deaths,
            "severity": severity,
            "spread_risk": "moderate",
            "lifecycle_stage": "active",
            "claim_type": "official",
            "confidence_score": 0.8,
            "verification_status": "unverified",
            "source_urls": ["https://example.org/a"],
            "source_names": ["WHO DON"],
            "ai_summary": "Summary text.",
            "neighboring_mentions": [],
            "first_seen": now - timedelta(days=days_ago + 1),
            "last_seen": now - timedelta(days=days_ago),
            "updated_at": now - timedelta(days=days_ago),
            "snapshots": snapshots
            or [{"date": "2026-11-18", "cases": cases, "deaths": deaths,
                 "confidence": 0.8, "severity": severity,
                 "source_url": "https://example.org/a"}],
        }
    )


# ── Listing ──────────────────────────────────────────────────

def test_outbreaks_returns_canonical_shape(client):
    seed()
    body = client.get("/outbreaks").json()

    assert len(body) == 1
    row = body[0]
    assert row["cases"] == 100
    assert row["deaths"] == 2
    assert row["severity"] == "moderate"
    assert row["incident_id"]
    assert row["snapshots"][0]["cases"] == 100


def test_critical_outbreaks_sort_first(client):
    seed(severity="low", disease="Cholera")
    seed(severity="critical", disease="Ebola", country="Uganda")

    body = client.get("/outbreaks").json()
    assert body[0]["disease"] == "Ebola"


def test_days_filter_excludes_old_outbreaks(client):
    seed(disease="Fresh", days_ago=1)
    seed(disease="Stale", country="Malawi", days_ago=90)

    recent = client.get("/outbreaks?days=7").json()
    assert [r["disease"] for r in recent] == ["Fresh"]

    wide = client.get("/outbreaks?days=180").json()
    assert len(wide) == 2


def test_search_matches_disease(client):
    seed(disease="Cholera")
    seed(disease="Measles", country="Malawi")

    body = client.get("/outbreaks?q=measles").json()
    assert [r["disease"] for r in body] == ["Measles"]


def test_region_filter_matches_country(client):
    seed(country="Zambia")
    seed(country="Malawi", disease="Measles")

    body = client.get("/outbreaks?region=Malawi").json()
    assert len(body) == 1
    assert body[0]["country"] == "Malawi"


def test_region_all_is_ignored(client):
    seed(country="Zambia")
    seed(country="Malawi", disease="Measles")

    assert len(client.get("/outbreaks?region=all").json()) == 2


def test_severity_filter_accepts_a_list(client):
    seed(severity="critical", disease="Ebola")
    seed(severity="low", disease="Cholera")

    body = client.get("/outbreaks?severity=critical,high").json()
    assert [r["disease"] for r in body] == ["Ebola"]


def test_limit_is_respected(client):
    for i in range(5):
        seed(disease=f"Disease{i}", country=f"Country{i}")

    assert len(client.get("/outbreaks?limit=2").json()) == 2


# ── Detail ───────────────────────────────────────────────────

def test_detail_returns_snapshots(client):
    seed()
    incident_id = client.get("/outbreaks").json()[0]["incident_id"]

    body = client.get(f"/outbreaks/{incident_id}").json()
    assert body["snapshots"][0]["cases"] == 100
    assert body["incident_key"] == "cholera:zambia"


def test_bad_id_is_404_not_500(client):
    assert client.get("/outbreaks/not-an-object-id").status_code == 404


def test_unknown_id_is_404(client):
    assert client.get("/outbreaks/507f1f77bcf86cd799439011").status_code == 404


# ── Stats / health ───────────────────────────────────────────

def test_stats_counts_by_severity(client):
    seed(severity="critical", disease="Ebola", cases=500, deaths=50)
    seed(severity="moderate", disease="Cholera", cases=100, deaths=2)

    body = client.get("/stats").json()
    assert body["critical"] == 1
    assert body["moderate"] == 1
    assert body["total"] == 2
    assert body["cases"] == 600
    assert body["deaths"] == 52
    assert set(body["countries"]) == {"Zambia"}


def test_health_reports_counts_and_run_state(client):
    seed()
    body = client.get("/health").json()

    assert body["outbreaks"] == 1
    assert "last_run" in body


# ── Writes are protected ─────────────────────────────────────

def test_detect_without_secret_is_401(client):
    resp = client.post("/outbreaks/detect", json={"disease": "X", "country": "Y"})
    assert resp.status_code == 401


def test_detect_with_secret_upserts(client):
    payload = {"disease": "Cholera", "country": "Zambia", "estimated_cases": 42}

    first = client.post("/outbreaks/detect?secret=test-secret", json=payload)
    assert first.status_code == 200
    assert first.json()["created"] is True

    payload["estimated_cases"] = 60
    second = client.post("/outbreaks/detect?secret=test-secret", json=payload)
    assert second.json()["created"] is False

    assert db.outbreaks.count_documents({}) == 1
    assert db.outbreaks.find_one({})["estimated_cases"] == 60


def test_detect_accepts_header_secret(client):
    resp = client.post(
        "/outbreaks/detect",
        json={"disease": "Cholera", "country": "Zambia"},
        headers={"x-cron-secret": "test-secret"},
    )
    assert resp.status_code == 200


def test_detect_rejects_missing_fields(client):
    resp = client.post("/outbreaks/detect?secret=test-secret", json={"disease": "Cholera"})
    assert resp.status_code == 400


def test_verify_worker_requires_secret(client):
    assert client.post("/workers/abc/verify").status_code == 401


# ── Delete ───────────────────────────────────────────────────

def test_delete_requires_secret(client):
    seed()
    incident_id = client.get("/outbreaks").json()[0]["incident_id"]

    assert client.delete(f"/outbreaks/{incident_id}").status_code == 401


def test_delete_removes_one_document(client):
    seed()
    incident_id = client.get("/outbreaks").json()[0]["incident_id"]

    resp = client.delete(f"/outbreaks/{incident_id}?secret=test-secret")
    assert resp.status_code == 200
    assert db.outbreaks.count_documents({}) == 0


# ── Database unreachable (the Render crash-loop case) ───────

def test_api_boots_when_mongo_dns_fails(monkeypatch):
    """A deleted Atlas cluster raises inside MongoClient's constructor.
    That must not take the process down — /health has to be able to report it.
    """
    import importlib
    import pymongo

    message = (
        "The DNS query name does not exist: "
        "_mongodb._tcp.crisiswatch.ozntwfq.mongodb.net."
    )

    def boom(*args, **kwargs):
        raise pymongo.errors.ConfigurationError(message)

    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://u:p@crisiswatch.ozntwfq.mongodb.net/")
    monkeypatch.setattr(pymongo, "MongoClient", boom)

    try:
        importlib.reload(db)

        assert db.client is None
        assert db.outbreaks is None
        assert "crisiswatch.ozntwfq.mongodb.net" in db.connection_error()

        import importlib as _il
        _il.reload(api)

        client = TestClient(api.app)
        health = client.get("/health")

        assert health.status_code == 200
        assert health.json()["db_connected"] is False
        assert "does not exist" in health.json()["db_error"]
        assert health.json()["status"] == "degraded"

        # And a data endpoint explains itself instead of returning a bare 500.
        assert client.get("/outbreaks").status_code == 503
    finally:
        monkeypatch.delenv("MONGODB_URI", raising=False)
        monkeypatch.undo()
        importlib.reload(db)
        importlib.reload(api)
