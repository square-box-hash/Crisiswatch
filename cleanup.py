from db import db

collections_to_clear = [
    "outbreaks",
    "cluster_signals",
    "worker_reviews",
    "spread_predictions",
    "safety_advice",
    "alert_versions"
]

for col in collections_to_clear:
    deleted = db[col].delete_many({})
    print(f"{col}: {deleted.deleted_count} documents removed")

print("Database reset complete.")