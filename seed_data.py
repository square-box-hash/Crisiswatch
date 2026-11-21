"""Insert demo outbreaks so the dashboard has something to render offline.

These rows are marked demo:true and are NOT live data. Run the pipeline for
real cases. Seeding is opt-in:

    python seed_data.py --yes
"""

import sys
from datetime import datetime, timedelta, timezone

import db
from pipeline import derive_severity
from models import OutbreakLifecycle, SpreadRisk
from store import incident_key

DEMO = [
    {
        "disease": "Cholera",
        "country": "Zambia",
        "cases": 412,
        "deaths": 11,
        "spread_risk": SpreadRisk.HIGH,
        "lifecycle": OutbreakLifecycle.ACTIVE,
        "summary": "Demo row. Cholera transmission continues in Lusaka province "
                   "following flooding; oral cholera vaccine campaign underway.",
    },
    {
        "disease": "Mpox",
        "country": "Democratic Republic of the Congo",
        "cases": 1870,
        "deaths": 24,
        "spread_risk": SpreadRisk.HIGH,
        "lifecycle": OutbreakLifecycle.ACTIVE,
        "summary": "Demo row. Clade I mpox transmission persists across several "
                   "provinces with household clustering reported.",
    },
    {
        "disease": "Measles",
        "country": "Yemen",
        "cases": 96,
        "deaths": 2,
        "spread_risk": SpreadRisk.MODERATE,
        "lifecycle": OutbreakLifecycle.CONFIRMED,
        "summary": "Demo row. Measles clusters reported among displaced families; "
                   "vaccination coverage remains below target.",
    },
]


def main() -> int:
    if db.outbreaks is None:
        print("MONGODB_URI is not set — cannot seed.")
        return 1

    if "--yes" not in sys.argv:
        print("This inserts demo rows (not live data). Re-run with --yes.")
        return 2

    db.ensure_indexes()
    now = datetime.now(timezone.utc)

    for entry in DEMO:
        population = 20_000_000
        severity = derive_severity(
            entry["spread_risk"], entry["lifecycle"],
            entry["cases"], entry["deaths"], population,
        )
        key = incident_key(entry["disease"], entry["country"])

        db.outbreaks.update_one(
            {"incident_key": key},
            {
                "$set": {
                    "incident_key": key,
                    "disease": entry["disease"],
                    "country": entry["country"],
                    "region": entry["country"],
                    "population": population,
                    "estimated_cases": entry["cases"],
                    "estimated_deaths": entry["deaths"],
                    "peak_cases": entry["cases"],
                    "severity": severity.value,
                    "spread_risk": entry["spread_risk"].value,
                    "lifecycle_stage": entry["lifecycle"].value,
                    "claim_type": "official",
                    "confidence_score": 0.8,
                    "verification_status": "unverified",
                    "source_urls": [],
                    "source_names": ["demo"],
                    "source_count": 0,
                    "ai_summary": entry["summary"],
                    "neighboring_mentions": [],
                    "first_seen": now - timedelta(days=3),
                    "last_seen": now,
                    "updated_at": now,
                    "demo": True,
                    "snapshots": [
                        {
                            "date": (now - timedelta(days=d)).strftime("%Y-%m-%d"),
                            "cases": max(1, int(entry["cases"] * (1 - d / 10))),
                            "deaths": entry["deaths"],
                            "confidence": 0.8,
                            "severity": severity.value,
                            "source_url": "",
                        }
                        for d in (2, 1, 0)
                    ],
                }
            },
            upsert=True,
        )

    print(f"Seeded {len(DEMO)} demo outbreaks (marked demo:true).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
