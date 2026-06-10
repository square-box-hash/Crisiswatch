from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime, timezone
from bson import ObjectId
import os

load_dotenv()

client = MongoClient(os.getenv("MONGODB_URI"))
db = client["crisiswatch"]

# Collections
outbreaks = db["outbreaks"]
cluster_signals = db["cluster_signals"]
worker_reviews = db["worker_reviews"]
alert_versions = db["alert_versions"]
spread_predictions = db["spread_predictions"]
safety_advice = db["safety_advice"]
health_workers = db["health_workers"]

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