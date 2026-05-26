from db import outbreaks, health_workers, update_alert_version
from models import OutbreakClassification, SeverityLevel
from datetime import datetime, timezone
from bson import ObjectId
import json

def register_worker(worker_data: dict) -> str:
    """Register a health worker — verified status starts False"""
    worker = {
        "name": worker_data["name"],
        "credentials": worker_data["credentials"],
        "license_number": worker_data["license_number"],
        "country": worker_data["country"],
        "institution": worker_data["institution"],
        "specialty": worker_data["specialty"],
        "verified": False,  # admin verifies manually
        "joined_at": datetime.now(timezone.utc)
    }
    result = health_workers.insert_one(worker)
    print(f"Worker registered: {worker_data['name']} (pending verification)")
    return str(result.inserted_id)

def verify_worker(worker_id: str) -> None:
    """Admin verifies a health worker"""
    health_workers.update_one(
        {"_id": ObjectId(worker_id)},
        {"$set": {"verified": True}}
    )
    print(f"Worker {worker_id} verified")

def submit_worker_message(
    outbreak_id: str,
    worker_id: str,
    message: str,
    classification_vote: str,
    severity_vote: str
) -> None:
    """Worker submits review on an active outbreak alert"""

    # Get worker details
    worker = health_workers.find_one({"_id": ObjectId(worker_id)})
    if not worker:
        print("Worker not found")
        return

    # Build message object
    worker_message = {
        "worker_name": worker["name"],
        "credentials": worker["credentials"],
        "institution": worker["institution"],
        "country": worker["country"],
        "verified": worker["verified"],
        "message": message,
        "classification_vote": classification_vote,
        "severity_vote": severity_vote,
        "submitted_at": datetime.now(timezone.utc)
    }

    # Push message to outbreak document
    outbreaks.update_one(
        {"_id": ObjectId(outbreak_id)},
        {
            "$push": {"worker_messages": worker_message},
            "$set": {"updated_at": datetime.now(timezone.utc)}
        }
    )

    # Update AI summary based on worker input
    update_ai_summary(outbreak_id)
    print(f"Message submitted by {worker['name']} ({'verified' if worker['verified'] else 'unverified'})")

def update_ai_summary(outbreak_id: str) -> None:
    """Rebuild AI summary from all worker messages — Gemini will do this for real"""
    outbreak = outbreaks.find_one({"_id": ObjectId(outbreak_id)})
    if not outbreak:
        return

    messages = outbreak.get("worker_messages", [])
    if not messages:
        return

    # Count votes
    classification_votes = {}
    severity_votes = {}
    verified_count = 0

    for msg in messages:
        cv = msg["classification_vote"]
        sv = msg["severity_vote"]
        classification_votes[cv] = classification_votes.get(cv, 0) + 1
        severity_votes[sv] = severity_votes.get(sv, 0) + 1
        if msg["verified"]:
            verified_count += 1

    # Find consensus
    top_classification = max(classification_votes, key=classification_votes.get)
    top_severity = max(severity_votes, key=severity_votes.get)

    # Build summary — Gemini will make this richer later
    summary = (
        f"{len(messages)} health worker(s) have reviewed this alert "
        f"({verified_count} verified). "
        f"Consensus classification: {top_classification}. "
        f"Consensus severity: {top_severity}."
    )

    # Update outbreak with new summary and consensus
    outbreaks.update_one(
        {"_id": ObjectId(outbreak_id)},
        {
            "$set": {
                "ai_summary": summary,
                "classification": top_classification,
                "severity": top_severity,
                "version": outbreak["version"] + 1,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )
    print(f"Alert updated to version {outbreak['version'] + 1}")
    print(f"Summary: {summary}")