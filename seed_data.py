from db import outbreaks
from datetime import datetime, timedelta, timezone

# Historical cholera outbreaks during monsoon in Dhaka
historical = [
    {
        "disease": "Cholera",
        "region": "Dhaka",
        "country": "Bangladesh",
        "severity": "moderate",
        "classification": "regular",
        "cluster_signal": {
            "disease": "Cholera",
            "region": "Dhaka",
            "country": "Bangladesh",
            "cases": 98,
            "deaths": 3,
            "population": 9000000,
            "case_rate": 1.09,
            "anomaly_score": 0.3,
            "trigger": "monsoon",
            "source_urls": []
        },
        "created_at": datetime.now(timezone.utc) - timedelta(days=365),
        "updated_at": datetime.now(timezone.utc) - timedelta(days=365),
        "version": 1,
        "worker_messages": [],
        "ai_summary": "",
        "confidence_score": 0.3
    },
    {
        "disease": "Cholera",
        "region": "Dhaka",
        "country": "Bangladesh",
        "severity": "moderate",
        "classification": "regular",
        "cluster_signal": {
            "disease": "Cholera",
            "region": "Dhaka",
            "country": "Bangladesh",
            "cases": 145,
            "deaths": 5,
            "population": 9000000,
            "case_rate": 1.61,
            "anomaly_score": 0.35,
            "trigger": "monsoon",
            "source_urls": []
        },
        "created_at": datetime.now(timezone.utc) - timedelta(days=730),
        "updated_at": datetime.now(timezone.utc) - timedelta(days=730),
        "version": 1,
        "worker_messages": [],
        "ai_summary": "",
        "confidence_score": 0.35
    }
]

outbreaks.insert_many(historical)
print("Historical data seeded")