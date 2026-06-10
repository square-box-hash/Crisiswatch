"""
DEPRECATED MODULE
────────────────────────────────────────────
This module is no longer part of the production pipeline.

It is kept for:
- experimentation
- research
- fallback reasoning
- MCP tool testing

DO NOT use in production flows.
"""

import json
import os
from datetime import datetime, timezone

from google import genai
from google.genai import types

# ─────────────────────────────────────────────
# Gemini Setup (sandbox only)
# ─────────────────────────────────────────────

_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))


def gemini_reason(prompt: str) -> str:
    """
    Experimental Gemini reasoning layer.
    NOT used in production pipeline.
    """
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

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        return text.strip()

    except Exception as e:
        return json.dumps({
            "error": str(e),
            "fallback": True
        })


# ─────────────────────────────────────────────
# LEGACY LOGIC (NOT USED IN PRODUCTION)
# ─────────────────────────────────────────────

def legacy_anomaly_score(cases: int, population: int) -> float:
    """Old anomaly scoring model (replaced by SRag pipeline)"""
    if population == 0:
        return 0.0
    rate = (cases / population) * 100000
    return min(rate / 50.0, 1.0)


def legacy_severity(case_rate: float, deaths: int, cases: int):
    """Old heuristic severity model"""
    cfr = deaths / cases if cases > 0 else 0

    if case_rate > 80 or cfr > 0.1:
        return "critical"
    elif case_rate > 50:
        return "high"
    elif case_rate > 20:
        return "moderate"
    return "low"


def legacy_classification(history: list, trigger: str):
    """Old outbreak classification logic"""
    if not history:
        return "novel"

    if trigger:
        return "regular"

    return "pending"


# ─────────────────────────────────────────────
# EXPERIMENTAL MCP ENTRYPOINT (NOT USED)
# ─────────────────────────────────────────────

async def run_agent_experiment(signal_data: dict):
    """
    MCP-based experimental outbreak reasoning.
    NOT connected to pipeline or API.
    """

    prompt = f"""
Analyze outbreak signal:

{json.dumps(signal_data, indent=2)}

Return:
- classification
- severity
- reasoning
"""

    return gemini_reason(prompt)


# ─────────────────────────────────────────────
# LEGACY ENTRY POINT (DISABLED)
# ─────────────────────────────────────────────

def process_cluster(signal_data: dict):
    """
    ⚠️ DEPRECATED
    Do not use in production pipeline.

    Kept only for backward compatibility.
    """
    incident_id = str(ObjectId())  # or uuid if you prefer

    return {
        "status": "deprecated",
        "message": "Use pipeline.py instead",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }