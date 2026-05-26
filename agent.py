from models import (
    ClusterSignal, OutbreakAlert, WorkerMessage,
    SeverityLevel, OutbreakClassification, AudienceType
)
from db import (
    save_cluster_signal, save_outbreak_alert,
    get_outbreak_history, get_seasonal_patterns,
    update_alert_version
)
from datetime import datetime, timedelta, timezone
import json

# ─── MOCK GEMINI (replace with real API once GCP credits arrive) ───

def gemini_reason(prompt: str) -> str:
    """Mock Gemini call — replace with Vertex AI later"""
    return json.dumps({
        "severity": "high",
        "assessment": "Mock assessment — Gemini not connected yet",
        "spread_regions": ["region_a", "region_b"],
        "confidence": 0.75
    })

# ─── CORE AGENT LOGIC ───

def calculate_anomaly_score(signal: ClusterSignal, history: list) -> float:
    """Compare current case rate against historical baseline"""
    if not history:
        return 0.8  # no history = potentially novel = high anomaly
    
    historical_rates = [
        h["cluster_signal"]["case_rate"] 
        for h in history 
        if "cluster_signal" in h
    ]
    
    if not historical_rates:
        return 0.8
        
    avg_rate = sum(historical_rates) / len(historical_rates)
    
    if avg_rate == 0:
        return 0.9
        
    ratio = signal.case_rate / avg_rate
    return min(ratio / 10, 1.0)  # normalize to 0-1

def classify_outbreak(signal: ClusterSignal, history: list) -> OutbreakClassification:
    """Novel vs Regular based on history and trigger matching"""
    if not history:
        return OutbreakClassification.NOVEL
    
    # check if seasonal trigger matches historical pattern
    if signal.trigger:
        seasonal_matches = get_seasonal_patterns(signal.disease, signal.trigger)
        if seasonal_matches:
            return OutbreakClassification.REGULAR
    
    # high anomaly with no matching pattern = novel
    if signal.anomaly_score > 0.7:
        return OutbreakClassification.NOVEL
        
    return OutbreakClassification.PENDING

def assess_severity(signal: ClusterSignal) -> SeverityLevel:
    """Severity based on case rate and anomaly score"""
    score = signal.anomaly_score
    cfr = signal.deaths / signal.cases if signal.cases > 0 else 0
    
    if score > 0.8 or cfr > 0.1:
        return SeverityLevel.CRITICAL
    elif score > 0.6 or cfr > 0.05:
        return SeverityLevel.HIGH
    elif score > 0.4 or cfr > 0.02:
        return SeverityLevel.MODERATE
    else:
        return SeverityLevel.LOW

def generate_safety_advice(disease: str, severity: SeverityLevel) -> dict:
    """Audience-specific safety advice per disease"""
    # placeholder — Gemini will generate this dynamically later
    return {
        AudienceType.PUBLIC.value: f"Stay informed about {disease}. Follow local health authority guidance.",
        AudienceType.PRACTITIONER.value: f"Review {disease} clinical protocols. Report clusters immediately.",
        AudienceType.NGO.value: f"Assess logistics for {disease} response in affected regions."
    }

def predict_spread(signal: ClusterSignal, classification: OutbreakClassification) -> dict:
    """Gemini-powered spread prediction — mocked for now"""
    prompt = f"""
    Disease: {signal.disease}
    Region: {signal.region}, {signal.country}
    Cases: {signal.cases}, Deaths: {signal.deaths}
    Population: {signal.population}
    Classification: {classification.value}
    Trigger: {signal.trigger or 'unknown'}
    
    Predict likely spread regions based on:
    - Population movement corridors
    - Climate zone similarity  
    - Transmission mode
    - Historical outbreak trajectories
    
    Return JSON with: target_regions, confidence, reasoning, timeframe
    """
    raw = gemini_reason(prompt)
    return json.loads(raw)

def process_cluster(signal_data: dict) -> str:
    """Main agent entry point — processes a detected cluster"""
    
    # Build signal object
    signal = ClusterSignal(**signal_data)
    signal.case_rate = (signal.cases / signal.population) * 100000
    
    # Get history from MongoDB
    history = get_outbreak_history(signal.disease, signal.region)
    
    # Core reasoning
    signal.anomaly_score = calculate_anomaly_score(signal, history)
    classification = classify_outbreak(signal, history)
    severity = assess_severity(signal)
    spread = predict_spread(signal, classification)
    advice = generate_safety_advice(signal.disease, severity)
    
    # Build alert
    alert = {
        "disease": signal.disease,
        "region": signal.region,
        "country": signal.country,
        "severity": severity.value,
        "classification": classification.value,
        "ai_assessment": f"Anomaly score {signal.anomaly_score:.2f}. Classification: {classification.value}.",
        "spread_prediction": spread,
        "safety_advice": advice,
        "cluster_signal": signal_data,
        "version": 1,
        "worker_messages": [],
        "ai_summary": "",
        "confidence_score": signal.anomaly_score,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }
    
    # Save to MongoDB
    save_cluster_signal(signal_data)
    outbreak_id = save_outbreak_alert(alert)
    
    print(f"Alert created: {outbreak_id}")
    print(f"Disease: {signal.disease} | Severity: {severity.value} | Classification: {classification.value}")
    
    return outbreak_id