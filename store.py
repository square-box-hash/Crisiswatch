"""
MongoDB persistence.

One document per outbreak, keyed on `incident_key` (disease + country, no date
in the key), with a `snapshots` array so the case count is tracked over time
instead of being overwritten. Re-running the pipeline updates the same
document — it does not create a second copy.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import db

log = logging.getLogger(__name__)

MAX_SNAPSHOTS = 90


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return value[:60]


def incident_key(disease: str, country: str) -> str:
    """Stable across runs and across days — this is the identity of an outbreak."""

    return f"{_slug(disease)}:{_slug(country)}"


def _require_collection():
    if db.outbreaks is None:
        raise RuntimeError(
            db.connection_error()
            or "MONGODB_URI is not set — cannot persist. "
               "Set it in .env, or run the pipeline with --dry-run."
        )
    return db.outbreaks


def _snapshot(doc: dict, captured_at: datetime) -> dict:
    return {
        "date": captured_at.strftime("%Y-%m-%d"),
        "cases": int(doc.get("estimated_cases") or 0),
        "deaths": int(doc.get("estimated_deaths") or 0),
        "confidence": float(doc.get("confidence_score") or 0),
        "severity": doc.get("severity") or "low",
        "source_url": (doc.get("source_urls") or [""])[0],
        "captured_at": captured_at,
    }


def upsert_incident(doc: dict, now: Optional[datetime] = None) -> dict:
    """
    Insert or update one outbreak. Idempotent: running twice in a minute adds
    one snapshot, not two.

    Returns {"created", "updated", "snapshot_added", "cases_delta"}.
    """

    collection = _require_collection()
    now = now or datetime.now(timezone.utc)

    key = doc.get("incident_key") or incident_key(
        doc.get("disease", ""), doc.get("country", "")
    )
    doc["incident_key"] = key

    existing = collection.find_one(
        {"incident_key": key},
        {"snapshots": 1, "first_seen": 1, "peak_cases": 1, "estimated_cases": 1},
    )

    snap = _snapshot(doc, now)
    cases = int(doc.get("estimated_cases") or 0)
    peak = max(cases, int((existing or {}).get("peak_cases") or 0))

    fields = {k: v for k, v in doc.items() if k not in ("_id", "snapshots", "first_seen")}
    fields["last_seen"] = now
    fields["updated_at"] = now
    fields["peak_cases"] = peak

    if existing is None:
        fields["first_seen"] = now
        fields["snapshots"] = [snap]
        collection.insert_one(fields)
        log.info(f"[Store] new {doc.get('disease')} / {doc.get('country')} ({cases} cases)")
        return {"created": True, "updated": False, "snapshot_added": True, "cases_delta": cases}

    previous_cases = int(existing.get("estimated_cases") or 0)
    previous_snaps = existing.get("snapshots") or []

    collection.update_one({"incident_key": key}, {"$set": fields})

    # One snapshot per day: refresh today's entry if it exists, else append.
    same_day = [s for s in previous_snaps if s.get("date") == snap["date"]]

    if same_day:
        updated = [
            snap if s.get("date") == snap["date"] else s for s in previous_snaps
        ]
        collection.update_one(
            {"incident_key": key}, {"$set": {"snapshots": updated[-MAX_SNAPSHOTS:]}}
        )
        snapshot_added = False
    else:
        collection.update_one(
            {"incident_key": key},
            {"$push": {"snapshots": {"$each": [snap], "$slice": -MAX_SNAPSHOTS}}},
        )
        snapshot_added = True

    log.info(
        f"[Store] update {doc.get('disease')} / {doc.get('country')} "
        f"({previous_cases} → {cases} cases)"
    )

    return {
        "created": False,
        "updated": True,
        "snapshot_added": snapshot_added,
        "cases_delta": cases - previous_cases,
    }


# =============================================================================
# Run log — this is what answers "why did I get no live cases?"
# =============================================================================

def start_run() -> Optional[object]:
    if db.pipeline_runs is None:
        return None

    return db.pipeline_runs.insert_one(
        {
            "started_at": datetime.now(timezone.utc),
            "status": "running",
            "sources": {},
            "articles_fetched": 0,
            "items_extracted": 0,
            "incidents_created": 0,
            "incidents_updated": 0,
            "errors": [],
        }
    ).inserted_id


def finish_run(run_id, summary: dict) -> None:
    if db.pipeline_runs is None or run_id is None:
        return

    db.pipeline_runs.update_one(
        {"_id": run_id},
        {"$set": {**summary, "finished_at": datetime.now(timezone.utc)}},
    )


def mark_stale_runs(minutes: int = 15) -> int:
    """A run whose process died stays "running" forever. Close those out."""

    if db.pipeline_runs is None:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=minutes)

    result = db.pipeline_runs.update_many(
        {"status": "running", "started_at": {"$lt": cutoff}},
        {
            "$set": {"status": "abandoned", "finished_at": now},
            "$push": {
                "errors": (
                    "run never reported completion — the instance restarted or "
                    "died mid-run"
                )
            },
        },
    )

    return result.modified_count


def latest_run() -> Optional[dict]:
    if db.pipeline_runs is None:
        return None

    doc = db.pipeline_runs.find_one(sort=[("started_at", -1)])
    if not doc:
        return None

    doc["_id"] = str(doc["_id"])
    for field_name in ("started_at", "finished_at"):
        if isinstance(doc.get(field_name), datetime):
            doc[field_name] = doc[field_name].isoformat()

    return doc
