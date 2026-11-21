"""Drop every outbreak from the database.

    python cleanup.py --yes

Keeps health_workers, so registered reviewers survive a data reset.
"""

import sys

import db

COLLECTIONS = [
    "outbreaks",
    "cluster_signals",
    "worker_reviews",
    "spread_predictions",
    "safety_advice",
    "alert_versions",
]


def main() -> int:
    if db.db is None:
        print("MONGODB_URI is not set — nothing to clean.")
        return 1

    if "--yes" not in sys.argv:
        print("This deletes every outbreak. Re-run with --yes to confirm.")
        return 2

    for name in COLLECTIONS:
        deleted = db.db[name].delete_many({})
        print(f"{name}: {deleted.deleted_count} documents removed")

    print("Database reset complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
