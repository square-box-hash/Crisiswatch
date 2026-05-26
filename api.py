from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from db import outbreaks, health_workers
from agent import process_cluster
from worker import register_worker, verify_worker, submit_worker_message
from bson import ObjectId
from datetime import datetime, timezone
import json

app = FastAPI(title="CrisisWatch API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# ── helpers ──
def serialize(doc):
    """Recursively convert ObjectId and datetime to JSON-safe types"""
    if isinstance(doc, list):
        return [serialize(i) for i in doc]
    if isinstance(doc, dict):
        return {k: serialize(v) for k, v in doc.items()}
    if isinstance(doc, ObjectId):
        return str(doc)
    if isinstance(doc, datetime):
        return doc.isoformat()
    return doc

# ── Request models ──
class ClusterInput(BaseModel):
    disease: str
    region: str
    country: str
    cases: int
    deaths: int
    population: int
    trigger: str | None = None
    source_urls: list[str] = []

class WorkerInput(BaseModel):
    name: str
    credentials: str
    license_number: str
    country: str
    institution: str
    specialty: str

class MessageInput(BaseModel):
    worker_id: str
    message: str
    classification_vote: str
    severity_vote: str

# ── Endpoints ──

@app.get("/")
def root():
    return {"status": "CrisisWatch online"}

@app.get("/outbreaks")
def get_outbreaks():
    """Get all active outbreaks — public feed"""
    docs = list(outbreaks.find(
        {},
        sort=[("updated_at", -1)],
        limit=20
    ))
    return [serialize(d) for d in docs]

@app.get("/outbreaks/{outbreak_id}")
def get_outbreak(outbreak_id: str):
    """Get single outbreak with full worker messages"""
    doc = outbreaks.find_one({"_id": ObjectId(outbreak_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Outbreak not found")
    return serialize(doc)

@app.post("/outbreaks/detect")
def detect_outbreak(signal: ClusterInput):
    """Agent entry point — process a new cluster signal"""
    outbreak_id = process_cluster(signal.model_dump())
    return {"outbreak_id": outbreak_id, "status": "alert_created"}

@app.post("/workers/register")
def register(worker: WorkerInput):
    """Register a new health worker"""
    worker_id = register_worker(worker.model_dump())
    return {"worker_id": worker_id, "status": "pending_verification"}

@app.post("/workers/{worker_id}/verify")
def verify(worker_id: str):
    """Admin verifies a health worker"""
    verify_worker(worker_id)
    return {"status": "verified"}

@app.post("/outbreaks/{outbreak_id}/review")
def submit_review(outbreak_id: str, msg: MessageInput):
    """Health worker submits review on an outbreak"""
    submit_worker_message(
        outbreak_id=outbreak_id,
        worker_id=msg.worker_id,
        message=msg.message,
        classification_vote=msg.classification_vote,
        severity_vote=msg.severity_vote
    )
    return {"status": "review_submitted"}

@app.get("/outbreaks/{outbreak_id}/summary")
def get_summary(outbreak_id: str):
    """Get current AI summary and worker consensus"""
    doc = outbreaks.find_one(
        {"_id": ObjectId(outbreak_id)},
        {"ai_summary": 1, "version": 1, "classification": 1, "severity": 1, "worker_messages": 1}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return serialize(doc)