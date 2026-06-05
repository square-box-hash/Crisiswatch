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
import os
from google import genai
from google.genai import types
from mcp_agent import run_mcp_agent_with_tools
import asyncio

# ─── GEMINI SETUP ───

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

def gemini_reason(prompt: str) -> str:
    """Real Gemini call via Google AI Studio"""
    try:
        response = _client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=512,
            )
        )
        text = response.text.strip()
        # Strip markdown code fences if Gemini wraps JSON in them
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return text.strip()
    except Exception as e:
        print(f"[Gemini error] {e}")
        # Graceful fallback so the agent doesn't crash
        return json.dumps({
            "severity": "high",
            "assessment": f"Gemini unavailable: {str(e)}",
            "spread_regions": [],
            "confidence": 0.5
        })

# ─── CORE AGENT LOGIC ───

def calculate_anomaly_score(signal: ClusterSignal, history: list) -> float:
    """Compare current case rate against historical baseline"""
    if not history:
        return 0.8
    
    historical_rates = [
        h["cluster_signal"]["case_rate"] 
        for h in history 
        if "cluster_signal" in h and "case_rate" in h["cluster_signal"]
    ]
    
    if not historical_rates:
        return 0.8
        
    avg_rate = sum(historical_rates) / len(historical_rates)
    
    if avg_rate == 0:
        return 0.9
        
    ratio = signal.case_rate / avg_rate
    return min(ratio / 10, 1.0)

def classify_outbreak(signal: ClusterSignal, history: list) -> OutbreakClassification:
    """Novel vs Regular based on history and trigger matching"""
    if not history:
        return OutbreakClassification.NOVEL
    
    if signal.trigger:
        seasonal_matches = get_seasonal_patterns(signal.disease, signal.trigger)
        if seasonal_matches:
            return OutbreakClassification.REGULAR
    
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

def generate_safety_advice(disease: str, severity: SeverityLevel, region: str, classification: str) -> dict:
    """Gemini-generated audience-specific safety advice"""
    prompt = f"""
You are a public health advisory system.

Disease: {disease}
Region: {region}
Severity: {severity.value}
Classification: {classification}

Generate concise, actionable safety advice for three audiences.
Return ONLY valid JSON, no markdown, no preamble:
{{
  "public": "2-3 sentence advice for general public",
  "practitioner": "2-3 sentence advice for doctors and nurses",
  "ngo": "2-3 sentence advice for NGO field teams"
}}
"""
    try:
        raw = gemini_reason(prompt)
        parsed = json.loads(raw)
        return {
            AudienceType.PUBLIC.value: parsed.get("public", "Follow local health authority guidance."),
            AudienceType.PRACTITIONER.value: parsed.get("practitioner", f"Review {disease} clinical protocols."),
            AudienceType.NGO.value: parsed.get("ngo", f"Assess logistics for {disease} response.")
        }
    except Exception as e:
        print(f"[Safety advice parse error] {e} | raw: {raw}")
        return {
            AudienceType.PUBLIC.value: f"Stay informed about {disease}. Follow local health authority guidance.",
            AudienceType.PRACTITIONER.value: f"Review {disease} clinical protocols. Report clusters immediately.",
            AudienceType.NGO.value: f"Assess logistics for {disease} response in affected regions."
        }

def predict_spread(signal: ClusterSignal, classification: OutbreakClassification) -> dict:
    """Gemini-powered spread prediction"""
    prompt = f"""
You are an epidemiological spread modeling system.

Disease: {signal.disease}
Region: {signal.region}, {signal.country}
Cases: {signal.cases}, Deaths: {signal.deaths}
Population: {signal.population}
Case rate per 100k: {signal.case_rate:.2f}
Classification: {classification.value}
Trigger: {signal.trigger or 'unknown'}

Predict spread based on population movement corridors, climate zones,
transmission mode, and historical outbreak trajectories for this disease.

Return ONLY valid JSON, no markdown, no preamble:
{{
  "target_regions": ["region1", "region2"],
  "confidence": 0.0,
  "reasoning": "brief epidemiological reasoning",
  "timeframe": "e.g. 2-4 weeks"
}}
"""
    try:
        raw = gemini_reason(prompt)
        return json.loads(raw)
    except Exception as e:
        print(f"[Spread prediction parse error] {e} | raw: {raw}")
        return {
            "target_regions": [],
            "confidence": 0.5,
            "reasoning": "Gemini reasoning unavailable.",
            "timeframe": "unknown"
        }

def generate_ai_summary(signal: ClusterSignal, classification: OutbreakClassification,
                         severity: SeverityLevel, spread: dict) -> str:
    """Gemini-generated one-paragraph alert summary"""
    prompt = f"""
You are a disease surveillance officer writing an alert brief.

Disease: {signal.disease}
Location: {signal.region}, {signal.country}
Cases: {signal.cases}, Deaths: {signal.deaths}
Severity: {severity.value}
Classification: {classification.value}
Anomaly score: {signal.anomaly_score:.2f}
Predicted spread: {', '.join(spread.get('target_regions', [])) or 'undetermined'}
Timeframe: {spread.get('timeframe', 'unknown')}

Write a 3-sentence situational summary for health authorities.
Be factual, concise, and avoid alarmist language.
Return plain text only.
"""
    try:
        response = _client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=200)
        )
        return response.text.strip()
    except Exception as e:
        print(f"[AI summary error] {e}")
        return f"{signal.disease} outbreak detected in {signal.region}, {signal.country}. Severity: {severity.value}. Classification: {classification.value}."

def process_cluster(signal_data: dict) -> str:
    """Main agent entry point — processes a detected cluster"""
    
    signal_data_copy = {**signal_data}
    signal_data_copy.setdefault("case_rate", 0.0)
    signal_data_copy.setdefault("anomaly_score", 0.0)
    signal = ClusterSignal(**signal_data_copy)
    signal.case_rate = (signal.cases / signal.population) * 100000
    
    history = get_outbreak_history(signal.disease, signal.region)
    signal.anomaly_score = calculate_anomaly_score(signal, history)

    signal_data["case_rate"] = signal.case_rate
    signal_data["anomaly_score"] = signal.anomaly_score

    mcp_result = None
    try:
        from mcp_agent import run_mcp_agent_with_tools
        from db import db as mongo_db
        mcp_result = asyncio.run(run_mcp_agent_with_tools(signal_data, mongo_db))
    except Exception as e:
        print(f"[MCP Agent error] {e}")

    if mcp_result:
        try:
            classification = OutbreakClassification(mcp_result.get("classification", "pending").lower())
            severity = SeverityLevel(mcp_result.get("severity", "moderate").lower())
            spread = mcp_result.get("spread_prediction", {})
            advice_raw = mcp_result.get("safety_advice", {})
            advice = {
                AudienceType.PUBLIC.value: advice_raw.get("public", ""),
                AudienceType.PRACTITIONER.value: advice_raw.get("practitioner", ""),
                AudienceType.NGO.value: advice_raw.get("ngo", "")
            }
            ai_summary = mcp_result.get("ai_summary", "")
            ai_assessment = mcp_result.get("ai_assessment", f"Anomaly score {signal.anomaly_score:.2f}.")
            print(f"[MCP Agent] Reasoning complete — {classification.value}/{severity.value}")
        except Exception as e:
            print(f"[MCP Agent parse error] {e} - falling back")
            mcp_result = None

    if not mcp_result:
        classification = classify_outbreak(signal, history)
        severity = assess_severity(signal)
        spread = predict_spread(signal, classification)
        advice = generate_safety_advice(signal.disease, severity, signal.region, classification)
        ai_summary = generate_ai_summary(signal, classification, severity, spread)
        ai_assessment = f"Anomaly score {signal.anomaly_score:.2f}. Classification: {classification.value}."
    
    
    alert = {
        "disease": signal.disease,
        "region": signal.region,
        "country": signal.country,
        "severity": severity.value,
        "classification": classification.value,
        "ai_assessment": ai_assessment,
        "ai_summary": ai_summary,
        "spread_prediction": spread,
        "safety_advice": advice,
        "cluster_signal": signal_data,
        "version": 1,
        "worker_messages": [],
        "confidence_score": signal.anomaly_score,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }
    
    save_cluster_signal(signal_data)
    outbreak_id = save_outbreak_alert(alert)
    
    print(f"Alert created: {outbreak_id}")
    print(f"Disease: {signal.disease} | Severity: {severity.value} | Classification: {classification.value}")
    print(f"AI Summary: {ai_summary[:100]}...")
    
    return outbreak_id