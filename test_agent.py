# test_agent.py
from agent import process_cluster
from datetime import datetime, timedelta, timezone

# Simulate an Ebola cluster detection
test_signal = {
    "disease": "Ebola",
    "region": "North Kivu",
    "country": "DRC",
    "cases": 47,
    "deaths": 31,
    "population": 6800000,
    "case_rate": 0.0,       # calculated by agent
    "anomaly_score": 0.0,   # calculated by agent
    "trigger": None,        # no seasonal trigger = potentially novel
    "source_urls": ["https://who.int/mock"]
}

outbreak_id = process_cluster(test_signal)
print(f"Outbreak stored with ID: {outbreak_id}")

# Test 2 - Cholera (regular)
test_signal_2 = {
    "disease": "Cholera",
    "region": "Dhaka",
    "country": "Bangladesh",
    "cases": 120,
    "deaths": 4,
    "population": 9000000,
    "case_rate": 0.0,
    "anomaly_score": 0.0,
    "trigger": "monsoon",
    "source_urls": ["https://who.int/mock"]
}

outbreak_id_2 = process_cluster(test_signal_2)
print(f"Outbreak stored with ID: {outbreak_id_2}")