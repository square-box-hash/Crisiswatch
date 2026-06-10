from db import outbreaks, health_workers
from datetime import datetime, timezone
from bson import ObjectId


# ─────────────────────────────────────────────
# Register Worker
# ─────────────────────────────────────────────

def register_worker(worker_data: dict) -> str:
    """Register a health worker — starts unverified"""

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
    print(f"Worker registered: {worker_data['name']} (pending verification)")
    return str(result.inserted_id)


# ─────────────────────────────────────────────
# Verify Worker (Admin)
# ─────────────────────────────────────────────

def verify_worker(worker_id: str) -> None:
    """Admin verification step"""

    health_workers.update_one(
        {"_id": ObjectId(worker_id)},
        {"$set": {"verified": True}}
    )

    print(f"Worker {worker_id} verified")


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

    worker = health_workers.find_one({"_id": ObjectId(worker_id)})

    if not worker:
        print("Worker not found")
        return

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

    outbreaks.update_one(
        {"_id": ObjectId(outbreak_id)},
        {
            "$push": {"worker_messages": worker_message},
            "$set": {"updated_at": datetime.now(timezone.utc)}
        }
    )

    update_worker_consensus(outbreak_id)

    print(
        f"Message submitted by {worker['name']} "
        f"({'verified' if worker['verified'] else 'unverified'})"
    )


# ─────────────────────────────────────────────
# Consensus Builder (IMPORTANT CHANGE)
# ─────────────────────────────────────────────

def update_worker_consensus(outbreak_id: str) -> None:
    """Build consensus WITHOUT overriding AI outputs"""

    outbreak = outbreaks.find_one({"_id": ObjectId(outbreak_id)})

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

    # ─────────────────────────────────────────
    # IMPORTANT: DO NOT overwrite AI fields
    # ─────────────────────────────────────────

    outbreaks.update_one(
        {"_id": ObjectId(outbreak_id)},
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

    print(f"Consensus updated for outbreak {outbreak_id}")
    print(summary)