from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from bson import ObjectId

from db import outbreaks, health_workers
from worker import register_worker, verify_worker, submit_worker_message

import json

app = FastAPI(title="CrisisWatch API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

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
    """
    Convert DB document → frontend-safe canonical format
    (prevents schema drift issues)
    """
    return {
        "incident_id": str(doc.get("_id")),
        "disease": doc.get("disease"),
        "country": doc.get("country"),
        "region": doc.get("region"),

        "cases": doc.get("estimated_cases"),
        "deaths": doc.get("estimated_deaths"),

        "population": doc.get("population"),

        "severity": doc.get("severity"),
        "spread_risk": doc.get("spread_risk"),

        "lifecycle_stage": doc.get("lifecycle_stage"),

        "claim_type": doc.get("claim_type"),

        "confidence_score": doc.get("confidence_score"),

        "trigger": doc.get("trigger"),

        "source_urls": doc.get("source_urls", []),

        "ai_summary": doc.get("ai_summary"),
        "reasoning": doc.get("reasoning"),

        "neighboring_mentions": doc.get("neighboring_mentions", []),

        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


# ─────────────────────────────────────────────
# Root
# ─────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "CrisisWatch online"}


# ─────────────────────────────────────────────
# Outbreaks
# ─────────────────────────────────────────────

@app.get("/outbreaks")
def get_outbreaks():
    """Get latest outbreaks (frontend feed)"""
    docs = list(outbreaks.find(
        {},
        sort=[("updated_at", -1)],
        limit=20
    ))

    return [normalize_outbreak(d) for d in docs]

# ─────────────────────────────────────────────
# Pipeline Trigger (for UptimeRobot cron)
# ─────────────────────────────────────────────

import os
from fastapi import BackgroundTasks, Query
from pipeline import run_pipeline_sync

@app.get("/trigger-pipeline")
async def trigger_pipeline(
    background_tasks: BackgroundTasks,
    secret: str = Query(...)
):
    if secret != os.environ.get("CRON_SECRET"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    background_tasks.add_task(run_pipeline_sync)
    return {"status": "pipeline triggered"}


@app.get("/outbreaks/{outbreak_id}")
def get_outbreak(outbreak_id: str):
    """Get full outbreak detail"""
    doc = outbreaks.find_one({"_id": ObjectId(outbreak_id)})

    if not doc:
        raise HTTPException(status_code=404, detail="Outbreak not found")

    return serialize(doc)


# ─────────────────────────────────────────────
# NEW: Pipeline ingestion endpoint
# ─────────────────────────────────────────────

@app.post("/outbreaks/detect")
def detect_outbreak(incident: dict):
    """
    Receives CanonicalIncident from pipeline
    Stores directly into DB (no transformation logic here)
    """

    if not isinstance(incident, dict):
        raise HTTPException(status_code=400, detail="Invalid incident format")

    # basic validation safeguard
    if "incident_id" not in incident:
        raise HTTPException(status_code=400, detail="Missing incident_id")

    incident = serialize(incident)

    inserted = outbreaks.insert_one(incident)

    return {
        "outbreak_id": str(inserted.inserted_id),
        "status": "stored"
    }


# ─────────────────────────────────────────────
# Workers
# ─────────────────────────────────────────────

@app.post("/workers/register")
def register(worker: dict):
    worker_id = register_worker(worker)
    return {"worker_id": worker_id, "status": "pending_verification"}


@app.post("/workers/{worker_id}/verify")
def verify(worker_id: str):
    verify_worker(worker_id)
    return {"status": "verified"}


@app.post("/outbreaks/{outbreak_id}/review")
def submit_review(outbreak_id: str, msg: dict):
    submit_worker_message(
        outbreak_id=outbreak_id,
        worker_id=msg["worker_id"],
        message=msg["message"],
        classification_vote=msg["classification_vote"],
        severity_vote=msg["severity_vote"]
    )

    return {"status": "review_submitted"}


# ─────────────────────────────────────────────
# Summary endpoint
# ─────────────────────────────────────────────

@app.get("/outbreaks/{outbreak_id}/summary")
def get_summary(outbreak_id: str):

    doc = outbreaks.find_one(
        {"_id": ObjectId(outbreak_id)},
        {
            "ai_summary": 1,
            "version": 1,
            "classification": 1,
            "severity": 1,
            "worker_messages": 1
        }
    )

    if not doc:
        raise HTTPException(status_code=404, detail="Not found")

    return serialize(doc)