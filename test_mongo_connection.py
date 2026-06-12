"""
Quick test — insert an artificial outbreak via the API,
verify it appears, then optionally delete it.

Usage:
    python test_mongo_connection.py insert
    python test_mongo_connection.py check
    python test_mongo_connection.py delete
"""

import sys
import requests

API_BASE = "https://crisiswatch-ohrl.onrender.com"

TEST_INCIDENT = {
    "incident_id": "test_incident_001",
    "disease": "Test Disease (DELETE ME)",
    "region": "Testland",
    "country": "Testland",
    "population": 1_000_000,
    "estimated_cases": 42,
    "estimated_deaths": 1,
    "severity": "low",
    "spread_risk": "low",
    "lifecycle_stage": "emerging",
    "claim_type": "official",
    "confidence_score": 0.5,
    "verification_status": "verified",
    "trigger": None,
    "source_count": 1,
    "source_urls": ["https://test.example.com"],
    "neighboring_mentions": [],
    "ai_summary": "This is a TEST entry to verify MongoDB connection.",
    "reasoning": "Testing MongoDB connection from test script.",
    "evidence_session": None,
    "metadata": {},
}


def insert_test():
    resp = requests.post(f"{API_BASE}/outbreaks/detect", json=TEST_INCIDENT, timeout=60)
    print(f"Status: {resp.status_code}")
    print(resp.json())


def check_test():
    resp = requests.get(f"{API_BASE}/outbreaks", timeout=30)
    data = resp.json()
    outbreaks = data if isinstance(data, list) else data.get("outbreaks", data.get("data", []))

    found = [o for o in outbreaks if "DELETE ME" in o.get("disease", "")]

    if found:
        print(f"✓ Found {len(found)} test entry(ies):")
        for o in found:
            print(f"  _id: {o.get('_id')}")
            print(f"  disease: {o.get('disease')}")
            print(f"  country: {o.get('country')}")
    else:
        print("✗ No test entry found")

    print(f"\nTotal outbreaks in DB: {len(outbreaks)}")


def delete_test():
    # First find the _id
    resp = requests.get(f"{API_BASE}/outbreaks", timeout=30)
    data = resp.json()
    outbreaks = data if isinstance(data, list) else data.get("outbreaks", data.get("data", []))

    found = [o for o in outbreaks if "DELETE ME" in o.get("disease", "")]

    if not found:
        print("No test entries found to delete.")
        return

    for o in found:
        _id = o.get("_id")
        try:
            resp = requests.delete(f"{API_BASE}/outbreaks/{_id}", timeout=30)
            print(f"Delete {_id}: {resp.status_code}")
        except Exception as e:
            print(f"Failed to delete {_id}: {e}")
            print("Your backend may not have a DELETE endpoint — "
                  "delete manually via MongoDB Atlas instead.")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"

    if action == "insert":
        insert_test()
    elif action == "check":
        check_test()
    elif action == "delete":
        delete_test()
    else:
        print("Usage: python test_mongo_connection.py [insert|check|delete]")