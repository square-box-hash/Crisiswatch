from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
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

def save_cluster_signal(signal: dict) -> str:
    result = cluster_signals.insert_one(signal)
    return str(result.inserted_id)

def save_outbreak_alert(alert: dict) -> str:
    result = outbreaks.insert_one(alert)
    return str(result.inserted_id)

def get_outbreak_history(disease: str, region: str) -> list:
    return list(outbreaks.find(
        {"disease": disease, "region": region},
        sort=[("created_at", -1)],
        limit=10
    ))

def save_worker_message(outbreak_id: str, message: dict) -> None:
    outbreaks.update_one(
        {"_id": outbreak_id},
        {
            "$push": {"worker_messages": message},
            "$set": {"updated_at": datetime.now(timezone.utc)}
        }
    )

def update_alert_version(outbreak_id: str, updates: dict) -> None:
    updates["updated_at"] = datetime.now(timezone.utc)
    updates["$inc"] = {"version": 1}
    outbreaks.update_one({"_id": outbreak_id}, {"$set": updates})

def get_seasonal_patterns(disease: str, trigger: str) -> list:
    return list(outbreaks.find({
        "disease": disease,
        "cluster_signal.trigger": trigger,
        "classification": "regular"
    }))