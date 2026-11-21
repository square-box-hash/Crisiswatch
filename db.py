from pymongo import MongoClient, ASCENDING, DESCENDING
from dotenv import load_dotenv
from datetime import datetime, timezone
from bson import ObjectId
import os

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("MONGODB_DB", "crisiswatch")

client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000) if MONGODB_URI else None

db = client[DB_NAME] if client is not None else None

# Collections — None when MONGODB_URI is unset, so imports never crash.
outbreaks = db["outbreaks"] if db is not None else None
cluster_signals = db["cluster_signals"] if db is not None else None
worker_reviews = db["worker_reviews"] if db is not None else None
alert_versions = db["alert_versions"] if db is not None else None
spread_predictions = db["spread_predictions"] if db is not None else None
safety_advice = db["safety_advice"] if db is not None else None
health_workers = db["health_workers"] if db is not None else None
pipeline_runs = db["pipeline_runs"] if db is not None else None


def ping() -> bool:
    if client is None:
        return False
    try:
        client.admin.command("ping")
        return True
    except Exception:
        return False


def ensure_indexes() -> None:
    """Idempotent. One document per outbreak, keyed on incident_key."""

    if outbreaks is None:
        return

    outbreaks.create_index(
        [("incident_key", ASCENDING)],
        unique=True,
        name="uniq_incident_key",
        sparse=True,
    )
    outbreaks.create_index([("last_seen", DESCENDING)])
    outbreaks.create_index([("severity", ASCENDING)])
    outbreaks.create_index([("country", ASCENDING)])

    if pipeline_runs is not None:
        pipeline_runs.create_index([("started_at", DESCENDING)])


# ─────────────────────────────────────────────
# PURE STORAGE FUNCTIONS
# ─────────────────────────────────────────────

def save_cluster_signal(signal: dict) -> str:
    return str(cluster_signals.insert_one(signal).inserted_id)


def save_outbreak(alert: dict) -> str:
    return str(outbreaks.insert_one(alert).inserted_id)


def get_outbreak_history(disease: str, region: str) -> list:
    return list(outbreaks.find({
        "disease": disease,
        "region": region
    }).sort("created_at", -1).limit(10))


def get_outbreak_by_id(outbreak_id: str):
    return outbreaks.find_one({"_id": ObjectId(outbreak_id)})


def update_outbreak(outbreak_id: str, update: dict) -> None:
    outbreaks.update_one(
        {"_id": ObjectId(outbreak_id)},
        {"$set": update}
    )


def append_worker_message(outbreak_id: str, message: dict) -> None:
    outbreaks.update_one(
        {"_id": ObjectId(outbreak_id)},
        {
            "$push": {"worker_messages": message},
            "$set": {"updated_at": datetime.now(timezone.utc)}
        }
    )
