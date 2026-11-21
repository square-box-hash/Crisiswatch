from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
from typing import Optional
from bson import ObjectId
from bson.errors import InvalidId
import os
import re

import assess
import db
import store
from worker import register_worker, verify_worker, submit_worker_message

app = FastAPI(title="CrisisWatch API", version="5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

SEVERITY_RANK = {"critical": 0, "high": 1, "moderate": 2, "low": 3}


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def serialize(doc):
    """Convert Mongo + Python objects into JSON-safe format"""

    if isinstance(doc, list):
        return [serialize(i) for i in doc]
    if isinstance(doc, dict):
        return {k: serialize(v) for k, v in doc.items()}
    if isinstance(doc, ObjectId):
        return str(doc)
    if isinstance(doc, datetime):
        return doc.isoformat()
    return doc


def normalize_outbreak(doc: dict) -> dict:
    """One canonical shape for the frontend. Severity is already
    low|moderate|high|critical here — the frontend no longer translates."""

    snapshots = doc.get("snapshots") or []

    return {
        "incident_id": str(doc.get("_id")),
        "incident_key": doc.get("incident_key"),
        "disease": doc.get("disease"),
        "country": doc.get("country"),
        "region": doc.get("region"),

        # None means "no source stated a figure" — the UI must show that as
        # unknown, not as zero.
        "cases": doc.get("estimated_cases"),
        "deaths": doc.get("estimated_deaths"),
        "cases_unknown": bool(doc.get("cases_unknown", doc.get("estimated_cases") is None)),
        "cases_source": doc.get("cases_source"),
        "cases_as_of": doc.get("cases_as_of"),
        "peak_cases": doc.get("peak_cases"),
        "population": doc.get("population"),

        "status": doc.get("status") or "signal",
        "sources_agreeing": doc.get("sources_agreeing") or len(doc.get("source_names") or []),
        "is_active": assess.is_active(doc.get("last_seen")),
        "contradictions": ((doc.get("metadata") or {}).get("synthesis") or {}).get("contradictions", []),
        "agreement": ((doc.get("metadata") or {}).get("synthesis") or {}).get("agreement"),
        "evidence": [
            {
                "source_name": e.get("source_name"),
                "url": e.get("url"),
                "tier": e.get("tier"),
                "published_at": e.get("published_at"),
                "cases": e.get("cases"),
                "deaths": e.get("deaths"),
            }
            for e in (doc.get("evidence") or [])
        ],

        "severity": doc.get("severity") or "low",
        "spread_risk": doc.get("spread_risk"),
        "lifecycle_stage": doc.get("lifecycle_stage"),
        "claim_type": doc.get("claim_type"),
        "confidence_score": doc.get("confidence_score"),
        "verification_status": doc.get("verification_status"),

        "trigger": doc.get("trigger"),
        "source_urls": doc.get("source_urls", []),
        "source_names": doc.get("source_names", []),
        "source_count": doc.get("source_count") or len(doc.get("source_urls") or []),

        "ai_summary": doc.get("ai_summary"),
        "reasoning": doc.get("reasoning"),
        "neighboring_mentions": doc.get("neighboring_mentions", []),

        "first_seen": doc.get("first_seen"),
        "last_seen": doc.get("last_seen"),
        "published_at": doc.get("published_at"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),

        "snapshots": [
            {
                "date": s.get("date"),
                "cases": s.get("cases") or 0,
                "deaths": s.get("deaths") or 0,
            }
            for s in snapshots[-30:]
        ],

        "worker_messages": doc.get("worker_messages", []),
        "worker_consensus": doc.get("worker_consensus"),
        "worker_summary": doc.get("worker_summary"),
    }


def _require_secret(secret: Optional[str], header: Optional[str]) -> None:
    expected = os.getenv("CRON_SECRET")

    if not expected:
        raise HTTPException(
            status_code=503,
            detail="CRON_SECRET is not configured on the server",
        )

    supplied = secret or header

    if supplied != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _collection():
    if db.outbreaks is None:
        raise HTTPException(
            status_code=503,
            detail=db.connection_error() or "MONGODB_URI is not configured",
        )
    return db.outbreaks


def _object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="Outbreak not found")


# ─────────────────────────────────────────────
# Root / health
# ─────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "CrisisWatch online", "version": app.version}


@app.get("/health")
def health(response: Response):
    """What the dashboard (and you) can check when the feed looks static."""

    response.headers["Cache-Control"] = "no-store, max-age=0"

    payload = {
        "status": "ok",
        "db_connected": db.ping(),
        "db_error": db.connection_error(),
        "outbreaks": 0,
        "last_run": None,
        "gemini_key": bool(os.getenv("GEMINI_API_KEY")),
        "mongo_uri": bool(os.getenv("MONGODB_URI")),
    }

    if db.outbreaks is not None:
        payload["outbreaks"] = db.outbreaks.count_documents({})

    payload["last_run"] = store.latest_run()

    if not payload["db_connected"]:
        payload["status"] = "degraded"
        if not payload["db_error"]:
            payload["db_error"] = "MongoDB did not answer ping"

    return payload


# ─────────────────────────────────────────────
# Outbreaks
# ─────────────────────────────────────────────

@app.get("/outbreaks")
def get_outbreaks(
    days: int = Query(30, ge=1, le=3650),
    region: Optional[str] = None,
    q: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    """Latest outbreaks, filterable. Every param here actually filters."""

    collection = _collection()

    query: dict = {
        "last_seen": {"$gte": datetime.now(timezone.utc) - timedelta(days=days)}
    }

    if region and region.lower() not in ("all", ""):
        query["country"] = {"$regex": f"^{re.escape(region)}$", "$options": "i"}

    if q:
        pattern = re.escape(q.strip())
        query["$or"] = [
            {"disease": {"$regex": pattern, "$options": "i"}},
            {"country": {"$regex": pattern, "$options": "i"}},
            {"neighboring_mentions": {"$regex": pattern, "$options": "i"}},
        ]

    if severity and severity.lower() not in ("all", ""):
        wanted = [s.strip().lower() for s in severity.split(",") if s.strip()]
        query["severity"] = {"$in": wanted}

    if status and status.lower() not in ("all", ""):
        wanted = [s.strip().lower() for s in status.split(",") if s.strip()]
        query["status"] = {"$in": wanted}

    docs = list(collection.find(query).limit(limit * 3))

    # Severity first, then most recently seen.
    docs.sort(
        key=lambda d: (
            SEVERITY_RANK.get(str(d.get("severity", "")).lower(), 9),
            -(
                d.get("last_seen").timestamp()
                if isinstance(d.get("last_seen"), datetime)
                else 0
            ),
        )
    )

    return [normalize_outbreak(d) for d in docs[:limit]]


@app.get("/stats")
def get_stats():
    """Counts for the sidebar badges — computed server-side, one shape."""

    collection = _collection()

    counts = {"critical": 0, "high": 0, "moderate": 0, "low": 0}
    by_status = {"confirmed": 0, "probable": 0, "suspected": 0, "signal": 0}
    total_cases = 0
    total_deaths = 0
    unknown_counts = 0
    countries = set()

    for doc in collection.find(
        {},
        {
            "severity": 1,
            "status": 1,
            "estimated_cases": 1,
            "estimated_deaths": 1,
            "country": 1,
        },
    ):
        key = str(doc.get("severity", "")).lower()
        if key in counts:
            counts[key] += 1
        status_key = str(doc.get("status") or "").lower()
        if status_key in by_status:
            by_status[status_key] += 1

        if doc.get("estimated_cases") is None:
            unknown_counts += 1
        else:
            total_cases += int(doc.get("estimated_cases") or 0)
        total_deaths += int(doc.get("estimated_deaths") or 0)

        if doc.get("country"):
            countries.add(doc["country"])

    return {
        **counts,
        "total": sum(counts.values()),
        "by_status": by_status,
        "cases": total_cases,
        "deaths": total_deaths,
        "unknown_cases": unknown_counts,
        "countries": sorted(countries),
        "last_run": store.latest_run(),
    }


@app.get("/outbreaks/{outbreak_id}")
def get_outbreak(outbreak_id: str):
    """Full outbreak detail, including case history."""

    collection = _collection()
    doc = collection.find_one({"_id": _object_id(outbreak_id)})

    if not doc:
        raise HTTPException(status_code=404, detail="Outbreak not found")

    return serialize(doc)


@app.get("/outbreaks/{outbreak_id}/summary")
def get_summary(outbreak_id: str):

    collection = _collection()
    doc = collection.find_one(
        {"_id": _object_id(outbreak_id)},
        {
            "ai_summary": 1,
            "version": 1,
            "severity": 1,
            "worker_messages": 1,
            "worker_consensus": 1,
            "worker_summary": 1,
        },
    )

    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    return serialize(doc)


# ─────────────────────────────────────────────
# Ingestion (protected)
# ─────────────────────────────────────────────

@app.post("/outbreaks/detect")
def detect_outbreak(
    incident: dict,
    secret: Optional[str] = Query(None),
    x_cron_secret: Optional[str] = Header(None, alias="x-cron-secret"),
):
    """
    Receives a CanonicalIncident and upserts it: same disease + country updates
    the existing document and appends a case snapshot, instead of inserting a
    duplicate row every run.
    """

    _require_secret(secret, x_cron_secret)

    if not isinstance(incident, dict):
        raise HTTPException(status_code=400, detail="Invalid incident format")

    if "disease" not in incident or "country" not in incident:
        raise HTTPException(
            status_code=400, detail="Missing disease or country"
        )

    payload = serialize(incident)

    if not payload.get("incident_key"):
        payload["incident_key"] = store.incident_key(
            payload.get("disease", ""), payload.get("country", "")
        )

    try:
        result = store.upsert_incident(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return {"status": "stored", **result}


@app.post("/admin/migrate")
def admin_migrate(
    secret: Optional[str] = Query(None),
    x_cron_secret: Optional[str] = Header(None, alias="x-cron-secret"),
    dry_run: bool = False,
):
    """
    Backfill legacy documents (no incident_key / last_seen / snapshots).

    Exists because free Render instances have no shell, so the migration cannot
    be run from inside the container. Idempotent — safe to call twice.
    """

    _require_secret(secret, x_cron_secret)

    collection = _collection()

    if dry_run:
        return {"dry_run": True, "documents": collection.count_documents({})}

    from migrate import migrate

    result = migrate(collection)
    db.ensure_indexes()

    return {"status": "migrated", **result}


@app.delete("/outbreaks/{outbreak_id}")
def delete_outbreak(
    outbreak_id: str,
    secret: Optional[str] = Query(None),
    x_cron_secret: Optional[str] = Header(None, alias="x-cron-secret"),
):
    """Remove a single outbreak — for clearing test rows or bad extractions."""

    _require_secret(secret, x_cron_secret)

    collection = _collection()
    result = collection.delete_one({"_id": _object_id(outbreak_id)})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Outbreak not found")

    return {"status": "deleted", "id": outbreak_id}


@app.get("/trigger-pipeline")
async def trigger_pipeline(
    response: Response,
    background_tasks: BackgroundTasks,
    secret: Optional[str] = Query(None),
    x_cron_secret: Optional[str] = Header(None, alias="x-cron-secret"),
    limit: Optional[int] = None,
    wait: bool = False,
):
    """
    Cron entry point (UptimeRobot / Render cron).

    Default: queue the run and return immediately.
    `&wait=true`: run it inside the request and return the summary — use this
    when you need to see why a run produced nothing.
    """

    _require_secret(secret, x_cron_secret)

    # A GET that starts a job must never be served from a browser cache.
    response.headers["Cache-Control"] = "no-store, max-age=0"

    from pipeline import run_pipeline, run_pipeline_sync

    if wait:
        summary = await run_pipeline(max_articles=limit)
        return {"triggered": True, **summary}

    background_tasks.add_task(run_pipeline_sync, max_articles=limit)

    return {"status": "pipeline triggered", "limit": limit}


# ─────────────────────────────────────────────
# Workers
# ─────────────────────────────────────────────

@app.post("/workers/register")
def register(worker: dict):
    worker_id = register_worker(worker)
    return {"worker_id": worker_id, "status": "pending_verification"}


@app.post("/workers/{worker_id}/verify")
def verify(
    worker_id: str,
    secret: Optional[str] = Query(None),
    x_cron_secret: Optional[str] = Header(None, alias="x-cron-secret"),
):
    _require_secret(secret, x_cron_secret)

    try:
        verify_worker(worker_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="Worker not found")

    return {"status": "verified"}


@app.post("/outbreaks/{outbreak_id}/review")
def submit_review(outbreak_id: str, msg: dict):

    try:
        submit_worker_message(
            outbreak_id=outbreak_id,
            worker_id=msg["worker_id"],
            message=msg["message"],
            classification_vote=msg["classification_vote"],
            severity_vote=msg["severity_vote"],
        )
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="Outbreak or worker not found")
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Missing field: {exc}")

    return {"status": "review_submitted"}
