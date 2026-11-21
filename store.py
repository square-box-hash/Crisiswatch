"""
MongoDB persistence.

One document per outbreak, keyed on `incident_key` (disease + country, no date
in the key). Re-running the pipeline updates that document and appends one
`snapshots` entry per day.

Two rules this file enforces:

  * a known case count is never replaced by "unknown" — a later advisory that
    states no figure cannot erase the number WHO reported;
  * every source claim is kept in `evidence`, so the status bucket and the
    headline numbers can always be traced back to a URL.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import assess
import db

log = logging.getLogger(__name__)

MAX_SNAPSHOTS = 90
MAX_EVIDENCE = 40


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


def _snapshot(fields: dict, captured_at: datetime) -> dict:
    return {
        "date": captured_at.strftime("%Y-%m-%d"),
        # None means "no source stated a figure" — kept distinct from 0.
        "cases": fields.get("estimated_cases"),
        "deaths": fields.get("estimated_deaths"),
        "status": fields.get("status"),
        "confidence": float(fields.get("confidence_score") or 0),
        "severity": fields.get("severity") or "low",
        "source_url": fields.get("cases_source") or "",
        "captured_at": captured_at,
    }


def upsert_incident(doc: dict, now: Optional[datetime] = None) -> dict:
    """
    Insert or update one outbreak. Idempotent.

    Returns {"created", "updated", "snapshot_added", "cases_delta",
             "status", "cases_unknown"}.
    """

    collection = _require_collection()
    now = now or datetime.now(timezone.utc)

    key = doc.get("incident_key") or incident_key(
        doc.get("disease", ""), doc.get("country", "")
    )
    doc["incident_key"] = key

    existing = collection.find_one({"incident_key": key})

    # ── Merge evidence, then let it decide the figures ──
    merged_evidence = assess.merge_evidence(
        (existing or {}).get("evidence") or [],
        doc.get("evidence") or [],
    )[-MAX_EVIDENCE:]

    if merged_evidence:
        summary = assess.summarise_evidence(merged_evidence)
    else:
        # Caller posted a record with no source evidence (an external client,
        # a manual entry). Trust what they sent rather than blanking it.
        cases = doc.get("estimated_cases")
        summary = {
            "status": doc.get("status")
            or ("suspected" if cases is not None else "signal"),
            "cases": cases,
            "deaths": doc.get("estimated_deaths"),
            "cases_source": doc.get("cases_source"),
            "cases_as_of": doc.get("cases_as_of"),
            "cases_unknown": cases is None,
            "sources_agreeing": int(doc.get("sources_agreeing") or 0),
        }

    # Never downgrade: an unknown figure must not replace a known one.
    if summary["cases"] is None and existing and existing.get("estimated_cases") is not None:
        summary["cases"] = existing.get("estimated_cases")
        summary["cases_source"] = existing.get("cases_source")
        summary["cases_as_of"] = existing.get("cases_as_of")
        summary["cases_unknown"] = False

    if summary["deaths"] is None and existing and existing.get("estimated_deaths") is not None:
        summary["deaths"] = existing.get("estimated_deaths")

    cases = summary["cases"]

    fields = {
        k: v
        for k, v in doc.items()
        if k not in ("_id", "snapshots", "first_seen", "evidence")
    }

    fields.update(
        {
            "evidence": merged_evidence,
            "status": summary["status"],
            "estimated_cases": cases,
            "estimated_deaths": summary["deaths"],
            "cases_source": summary["cases_source"],
            "cases_as_of": summary["cases_as_of"],
            "cases_unknown": summary["cases_unknown"],
            "sources_agreeing": summary["sources_agreeing"],
            "source_count": len(merged_evidence),
            "source_urls": [e.get("url") for e in merged_evidence if e.get("url")],
            "source_names": sorted(
                {e.get("source_name") for e in merged_evidence if e.get("source_name")}
            ),
            "last_seen": now,
            "updated_at": now,
            "peak_cases": max(
                int(cases or 0), int((existing or {}).get("peak_cases") or 0)
            ),
        }
    )

    snap = _snapshot(fields, now)

    if existing is None:
        fields["first_seen"] = now
        fields["snapshots"] = [snap]
        collection.insert_one(fields)
        log.info(
            f"[Store] new {fields.get('disease')} / {fields.get('country')} "
            f"({_fmt(cases)} cases, {summary['status']})"
        )
        return {
            "created": True,
            "updated": False,
            "snapshot_added": True,
            "cases_delta": cases,
            "status": summary["status"],
            "cases_unknown": summary["cases_unknown"],
        }

    previous_cases = existing.get("estimated_cases")
    previous_snaps = existing.get("snapshots") or []

    collection.update_one({"incident_key": key}, {"$set": fields})

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

    delta = None
    if isinstance(cases, int) and isinstance(previous_cases, int):
        delta = cases - previous_cases

    log.info(
        f"[Store] update {fields.get('disease')} / {fields.get('country')} "
        f"({_fmt(previous_cases)} → {_fmt(cases)} cases, {summary['status']}, "
        f"{summary['sources_agreeing']} source(s))"
    )

    return {
        "created": False,
        "updated": True,
        "snapshot_added": snapshot_added,
        "cases_delta": delta,
        "status": summary["status"],
        "cases_unknown": summary["cases_unknown"],
    }


def _fmt(value) -> str:
    return "unknown" if value is None else str(value)


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


def active_run(minutes: int = 15) -> Optional[dict]:
    """A run started recently and still marked running — don't start another."""

    if db.pipeline_runs is None:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    return db.pipeline_runs.find_one(
        {"status": "running", "started_at": {"$gte": cutoff}},
        sort=[("started_at", -1)],
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
