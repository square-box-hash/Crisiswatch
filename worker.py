from datetime import datetime, timezone
from bson import ObjectId
from bson.errors import InvalidId
import logging

import db

log = logging.getLogger(__name__)


def _collections():
    if db.outbreaks is None or db.health_workers is None:
        raise RuntimeError("MONGODB_URI is not set — worker features need MongoDB")
    return db.outbreaks, db.health_workers


def _oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise ValueError(f"not a valid id: {value!r}")


# ─────────────────────────────────────────────
# Register Worker
# ─────────────────────────────────────────────

def register_worker(worker_data: dict) -> str:
    """Register a health worker — starts unverified"""

    _, health_workers = _collections()

    worker = {
        "name": worker_data["name"],
        "credentials": worker_data["credentials"],
        "license_number": worker_data["license_number"],
        "country": worker_data["country"],
        "institution": worker_data["institution"],
        "specialty": worker_data["specialty"],
        "verified": False,
        "joined_at": datetime.now(timezone.utc)
    }

    result = health_workers.insert_one(worker)
    log.info(f"Worker registered: {worker_data['name']} (pending verification)")
    return str(result.inserted_id)


# ─────────────────────────────────────────────
# Verify Worker (Admin)
# ─────────────────────────────────────────────

def verify_worker(worker_id: str) -> None:
    """Admin verification step"""

    _, health_workers = _collections()

    result = health_workers.update_one(
        {"_id": _oid(worker_id)},
        {"$set": {"verified": True}}
    )

    if result.matched_count == 0:
        raise ValueError(f"worker not found: {worker_id}")

    log.info(f"Worker {worker_id} verified")


# ─────────────────────────────────────────────
# Submit Worker Review
# ─────────────────────────────────────────────

def submit_worker_message(
    outbreak_id: str,
    worker_id: str,
    message: str,
    classification_vote: str,
    severity_vote: str
) -> None:
    """Worker submits structured review"""

    outbreaks, health_workers = _collections()

    worker = health_workers.find_one({"_id": _oid(worker_id)})

    if not worker:
        raise ValueError(f"worker not found: {worker_id}")

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

    result = outbreaks.update_one(
        {"_id": _oid(outbreak_id)},
        {
            "$push": {"worker_messages": worker_message},
            "$set": {"updated_at": datetime.now(timezone.utc)}
        }
    )

    if result.matched_count == 0:
        raise ValueError(f"outbreak not found: {outbreak_id}")

    update_worker_consensus(outbreak_id)

    log.info(
        f"Message submitted by {worker['name']} "
        f"({'verified' if worker['verified'] else 'unverified'})"
    )


# ─────────────────────────────────────────────
# Consensus Builder
# ─────────────────────────────────────────────

def update_worker_consensus(outbreak_id: str) -> None:
    """Build consensus WITHOUT overriding AI outputs"""

    outbreaks, _ = _collections()

    outbreak = outbreaks.find_one({"_id": _oid(outbreak_id)})

    if not outbreak:
        return

    messages = outbreak.get("worker_messages", [])
    if not messages:
        return

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

    # ── consensus results ──
    top_classification = max(classification_votes, key=classification_votes.get)
    top_severity = max(severity_votes, key=severity_votes.get)

    total_votes = len(messages)
    agreement_strength = max(classification_votes.values()) / total_votes

    summary = (
        f"{total_votes} health worker reviews collected. "
        f"{verified_count} verified professionals contributed. "
        f"Consensus classification: {top_classification}. "
        f"Consensus severity: {top_severity}. "
        f"Agreement strength: {agreement_strength:.2f}."
    )

    # IMPORTANT: DO NOT overwrite AI fields
    outbreaks.update_one(
        {"_id": _oid(outbreak_id)},
        {
            "$set": {
                "worker_consensus": {
                    "classification": top_classification,
                    "severity": top_severity,
                    "verified_votes": verified_count,
                    "total_votes": total_votes,
                    "agreement_strength": agreement_strength
                },
                "worker_summary": summary,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )

    log.info(f"Consensus updated for outbreak {outbreak_id}: {summary}")
