import asyncio
import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

import feedparser
import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from srag import SRag, SRagConfig

# =============================================================================
# CrisisWatch v3
# Retrieval-Grounded Crisis Intelligence Pipeline
# =============================================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Pipeline] %(message)s"
)

log = logging.getLogger(__name__)

# =============================================================================
# Environment
# =============================================================================

CRISISWATCH_API = os.getenv(
    "CRISISWATCH_API",
    "https://crisiswatch-ohrl.onrender.com"
)

GEMINI_API_URL = os.getenv("GEMINI_API_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# =============================================================================
# SRag Configuration
# =============================================================================

sr = SRag(
    config=SRagConfig(
        use_reranker=True,
        use_playwright=True,
        use_lexicon=True,
        use_reputation=True,
        use_quality_evaluator=True,
        use_recency_ranking=True,
        use_searxng=True,
        recency_weight=0.55,
        max_results=10,
        chunk_size=256,
        trace_timing=True,
    )
)

# =============================================================================
# Sources
# =============================================================================

FEEDS = {
    "WHO DON":
        "https://www.who.int/rss-feeds/news-english.xml",

    "ProMED":
        "https://promedmail.org/rss/feed.php",

    "ReliefWeb":
        "https://reliefweb.int/updates/rss.xml?primary_country=0&source=1600",
}

GDELT_URL = (
    "https://api.gdeltproject.org/api/v2/doc/doc"
    "?query=disease+outbreak+epidemic"
    "&mode=artlist"
    "&maxrecords=25"
    "&format=json"
    "&timespan=1d"
)

OPENMETEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=precipitation_sum,temperature_2m_max"
    "&forecast_days=7&timezone=auto"
)

# =============================================================================
# Climate Regions
# =============================================================================

CLIMATE_REGIONS = {
    "DRC": (-4.0, 21.7),
    "Nigeria": (9.0, 8.0),
    "Bangladesh": (23.7, 90.4),
    "Ethiopia": (9.0, 40.0),
    "Uganda": (1.3, 32.3),
    "Kenya": (0.0, 38.0),
    "India": (20.6, 79.0),
    "Pakistan": (30.4, 69.3),
    "Yemen": (15.55, 48.5),
    "Haiti": (18.97, -72.29),
}

# =============================================================================
# Reputation Scores
# =============================================================================

SOURCE_REPUTATION = {
    "WHO DON": 0.98,
    "CDC": 0.96,
    "ReliefWeb": 0.92,
    "ProMED": 0.90,
    "PubMed": 0.95,
    "GDELT": 0.42,
    "Unknown": 0.30,
}

# =============================================================================
# Disease Keywords
# =============================================================================

DISEASE_KEYWORDS = {
    "ebola": "Ebola",
    "marburg": "Marburg",
    "cholera": "Cholera",
    "mpox": "Mpox",
    "monkeypox": "Mpox",
    "dengue": "Dengue",
    "measles": "Measles",
    "yellow fever": "Yellow Fever",
    "lassa": "Lassa Fever",
    "nipah": "Nipah",
    "anthrax": "Anthrax",
    "rift valley": "Rift Valley Fever",
    "meningitis": "Meningitis",
    "avian influenza": "Avian Influenza",
    "h5n1": "Avian Influenza H5N1",
}

COUNTRY_KEYWORDS = [
    "drc",
    "democratic republic of congo",
    "uganda",
    "kenya",
    "ethiopia",
    "somalia",
    "sudan",
    "cameroon",
    "chad",
    "mali",
    "niger",
    "guinea",
    "sierra leone",
    "liberia",
    "bangladesh",
    "india",
    "pakistan",
    "indonesia",
    "philippines",
    "haiti",
    "yemen",
    "mozambique",
    "tanzania",
    "angola",
    "zambia",
    "nigeria",
]

# =============================================================================
# Population
# =============================================================================

POP = {
    "Nigeria": 220000000,
    "DRC": 100000000,
    "Ethiopia": 120000000,
    "Kenya": 55000000,
    "Uganda": 48000000,
    "Bangladesh": 170000000,
    "India": 1400000000,
    "Pakistan": 230000000,
    "Haiti": 11000000,
    "Yemen": 34000000,
}

# =============================================================================
# Runtime Memory
# =============================================================================

_seen_hashes: set[str] = set()

# =============================================================================
# Utility
# =============================================================================

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def normalize_country(country: str) -> str:

    mapping = {
        "drc": "DRC",
        "democratic republic of congo": "DRC",
    }

    return mapping.get(
        country.lower(),
        country.title()
    )

def is_duplicate(title: str, source: str) -> bool:

    h = _hash(f"{source}:{title.lower().strip()}")

    if h in _seen_hashes:
        return True

    _seen_hashes.add(h)
    return False

def deduplicate_paragraphs(text: str) -> str:

    seen = set()
    output = []

    for para in text.split("\n\n"):

        p = para.strip()

        if not p:
            continue

        if p in seen:
            continue

        seen.add(p)
        output.append(p)

    return "\n\n".join(output)

def detect_disease(text: str) -> Optional[str]:

    t = text.lower()

    for keyword, disease in DISEASE_KEYWORDS.items():
        if keyword in t:
            return disease

    return None

def detect_country(text: str) -> Optional[str]:

    t = text.lower()

    for c in COUNTRY_KEYWORDS:

        if f" {c} " in f" {t} ":
            return normalize_country(c)

    return None

def disease_session_name(disease: str) -> str:

    return (
        f"disease_{disease.lower()}"
        .replace(" ", "_")
        .replace("-", "_")
    )

# =============================================================================
# Discovery Layer
# =============================================================================

async def fetch_feed_candidates(
    client: httpx.AsyncClient
) -> list[dict]:

    candidates = []

    for source_name, url in FEEDS.items():

        try:

            resp = await client.get(url, timeout=20)

            feed = feedparser.parse(resp.text)

            for entry in feed.entries[:15]:

                title = entry.get("title", "")
                summary = entry.get("summary", "")
                link = entry.get("link", url)

                if is_duplicate(title, source_name):
                    continue

                combined = f"{title}\n{summary}"

                disease = detect_disease(combined)

                if not disease:
                    continue

                country = detect_country(combined)

                if not country:
                    continue

                candidates.append({
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "source_name": source_name,
                    "disease": disease,
                    "country": country,
                })

        except Exception as e:
            log.warning(f"[{source_name}] Feed error: {e}")

    return candidates

async def fetch_gdelt_candidates(
    client: httpx.AsyncClient
) -> list[dict]:

    candidates = []

    try:

        resp = await client.get(GDELT_URL, timeout=20)

        data = resp.json()

        for article in data.get("articles", [])[:10]:

            title = article.get("title", "")
            url = article.get("url", "")

            if is_duplicate(title, "GDELT"):
                continue

            disease = detect_disease(title)

            if not disease:
                continue

            country = detect_country(title)

            if not country:
                continue

            candidates.append({
                "title": title,
                "summary": "",
                "url": url,
                "source_name": "GDELT",
                "disease": disease,
                "country": country,
            })

    except Exception as e:
        log.warning(f"[GDELT] Error: {e}")

    return candidates

# =============================================================================
# SRag Evidence Layer
# =============================================================================

async def acquire_evidence(candidate: dict) -> Optional[dict]:

    try:

        disease = candidate["disease"]
        session = disease_session_name(disease)

        log.info(
            f"[SRag] Reading "
            f"{candidate['disease']} / {candidate['country']}"
        )

        # ---------------------------------------------------------------------
        # Full article acquisition
        # ---------------------------------------------------------------------

        read_result = await sr.read(candidate["url"])

        raw_text = ""

        if hasattr(read_result, "content"):
            raw_text = read_result.content
        else:
            raw_text = str(read_result)

        if not raw_text:
            return None

        cleaned_text = deduplicate_paragraphs(raw_text)

        # ---------------------------------------------------------------------
        # Index evidence into SRag memory
        # ---------------------------------------------------------------------

        await sr.ingest_text(
            text=cleaned_text,
            metadata={
                "disease": disease,
                "country": candidate["country"],
                "source_name": candidate["source_name"],
                "url": candidate["url"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            session=session
        )

        # ---------------------------------------------------------------------
        # Verification
        # ---------------------------------------------------------------------

        verification = await sr.verify(
            query=(
                f"{disease} outbreak "
                f"{candidate['country']} "
                f"{datetime.utcnow().year}"
            ),
            session=session
        )

        # ---------------------------------------------------------------------
        # Context construction
        # ---------------------------------------------------------------------

        context = await sr.context(
            question=(
                f"What is the current outbreak status "
                f"of {disease} in {candidate['country']}?"
            ),
            session=session,
            output_format="json"
        )

        return {
            "candidate": candidate,
            "raw_text": cleaned_text,
            "verification": verification,
            "context": context,
            "session": session,
        }

    except Exception as e:

        log.warning(
            f"[Evidence] "
            f"{candidate['disease']} "
            f"{candidate['country']} "
            f"failed: {e}"
        )

        return None

# =============================================================================
# Gemini Intelligence Layer
# =============================================================================

SYSTEM_PROMPT = """
You are an epidemiological intelligence analyst.

Your task:
- analyze outbreak evidence
- distinguish active outbreaks from preparedness
- detect historical references
- reconcile conflicting information
- estimate confidence
- identify primary affected country
- estimate spread risk
- determine operational lifecycle stage

Rules:
- use ONLY supplied evidence
- avoid speculation
- return STRICT JSON
- do not include markdown

Lifecycle stages:
- emerging
- suspected
- confirmed
- active
- contained
- resolved
- historical

Return schema:

{
  "outbreak_detected": bool,
  "disease": str,
  "primary_country": str,
  "neighboring_mentions": [str],
  "claim_type": str,
  "lifecycle_stage": str,
  "confidence": float,
  "estimated_cases": int,
  "estimated_deaths": int,
  "spread_risk": str,
  "summary": str,
  "reasoning": str
}
"""

async def synthesize_incident(
    client: httpx.AsyncClient,
    evidence: dict
) -> Optional[dict]:

    if not GEMINI_API_URL or not GEMINI_API_KEY:

        log.warning(
            "[Gemini] Missing GEMINI_API_URL or GEMINI_API_KEY"
        )

        return None

    try:

        payload = {
            "system_prompt": SYSTEM_PROMPT,
            "evidence": {
                "candidate": evidence["candidate"],
                "verification": str(evidence["verification"]),
                "context": evidence["context"],
                "raw_text": evidence["raw_text"][:12000],
            }
        }

        resp = await client.post(
            GEMINI_API_URL,
            headers={
                "Authorization": f"Bearer {GEMINI_API_KEY}"
            },
            json=payload,
            timeout=120
        )

        if resp.status_code != 200:

            log.warning(
                f"[Gemini] API error {resp.status_code}"
            )

            return None

        data = resp.json()

        if isinstance(data, str):
            data = json.loads(data)

        return data

    except Exception as e:

        log.warning(f"[Gemini] Synthesis failed: {e}")

        return None

# =============================================================================
# Climate Layer
# =============================================================================

async def fetch_climate_triggers(
    client: httpx.AsyncClient
) -> dict[str, str]:

    triggers = {}

    for region, (lat, lon) in CLIMATE_REGIONS.items():

        try:

            url = OPENMETEO_URL.format(
                lat=lat,
                lon=lon
            )

            resp = await client.get(url, timeout=15)

            data = resp.json()

            daily = data.get("daily", {})
            precip = daily.get(
                "precipitation_sum",
                [0]
            )

            max_precip = max(precip) if precip else 0

            if max_precip > 30:

                triggers[region] = "monsoon_flood"

                log.info(
                    f"[Climate] Trigger "
                    f"{region} ({max_precip}mm)"
                )

        except Exception as e:

            log.warning(
                f"[Climate] "
                f"{region} failed: {e}"
            )

    return triggers

# =============================================================================
# Incident Builder
# =============================================================================

from models import (
    CanonicalIncident,
    SeverityLevel,
    SpreadRisk,
    OutbreakLifecycle,
    ClaimType,
    VerificationStatus
)

def build_canonical_incident(
    synthesized: dict,
    evidence: dict,
    climate_triggers: dict[str, str]
) -> Optional[CanonicalIncident]:

    if not synthesized:
        return None

    if not synthesized.get("outbreak_detected"):
        return None

    disease = synthesized.get("disease")
    country = synthesized.get("primary_country")

    if not disease or not country:
        return None

    # ---------------------------
    # Confidence calibration
    # ---------------------------
    confidence = float(synthesized.get("confidence", 0.5))

    source_name = evidence["candidate"]["source_name"]

    confidence *= SOURCE_REPUTATION.get(source_name, 0.30)

    confidence = round(min(max(confidence, 0.05), 0.99), 3)

    # ---------------------------
    # Identity
    # ---------------------------
    incident_id = _hash(
        f"{disease}:{country}:{datetime.utcnow().date()}"
    )

    # ---------------------------
    # Enum-safe mappings (IMPORTANT)
    # ---------------------------
    try:
        lifecycle = OutbreakLifecycle(
            synthesized.get("lifecycle_stage", "active")
        )
    except:
        lifecycle = OutbreakLifecycle.ACTIVE

    try:
        claim_type = ClaimType(
            synthesized.get("claim_type", "official")
        )
    except:
        claim_type = ClaimType.OFFICIAL

    try:
        spread_risk = SpreadRisk(
            synthesized.get("spread_risk", "moderate")
        )
    except:
        spread_risk = SpreadRisk.MODERATE

    # ---------------------------
    # Build Canonical Object
    # ---------------------------
    return CanonicalIncident(
        incident_id=incident_id,

        disease=disease,
        region=country,
        country=country,

        population=POP.get(country, 10_000_000),

        estimated_cases=int(synthesized.get("estimated_cases", 0)),
        estimated_deaths=int(synthesized.get("estimated_deaths", 0)),

        severity=SeverityLevel.MODERATE,  # can refine later

        spread_risk=spread_risk,
        lifecycle_stage=lifecycle,
        claim_type=claim_type,

        confidence_score=confidence,

        verification_status=VerificationStatus.VERIFIED,

        trigger=climate_triggers.get(country),

        source_count=1,
        source_urls=[evidence["candidate"]["url"]],

        neighboring_mentions=synthesized.get("neighboring_mentions", []),

        ai_summary=synthesized.get("summary", ""),
        reasoning=synthesized.get("reasoning", ""),

        evidence_session=evidence.get("session"),

        metadata={}
    )

# =============================================================================
# Incident Reconciliation
# =============================================================================

def reconcile_signals(
    incidents: list[dict]
) -> list[dict]:

    grouped = defaultdict(list)

    for incident in incidents:

        key = (
            incident["disease"],
            incident["country"]
        )

        grouped[key].append(incident)

    merged = []

    for _, group in grouped.items():

        group.sort(
            key=lambda x: x["confidence"],
            reverse=True
        )

        primary = group[0]

        if len(group) > 1:

            primary["confidence"] = round(
                min(
                    primary["confidence"] +
                    (0.05 * (len(group) - 1)),
                    0.99
                ),
                3
            )

            primary["source_urls"] = list({
                u
                for g in group
                for u in g["source_urls"]
            })

        merged.append(primary)

    return merged

# =============================================================================
# Submission Layer
# =============================================================================

async def submit_incident(
    client: httpx.AsyncClient,
    incident: dict
) -> bool:

    try:

        resp = await client.post(
            f"{CRISISWATCH_API}/outbreaks/detect",
            json=incident,
            timeout=90
        )

        if resp.status_code == 200:

            log.info(
                f"✓ Submitted "
                f"{incident['disease']} / "
                f"{incident['country']} "
                f"(conf={incident['confidence']})"
            )

            return True

        log.warning(
            f"[Submit] "
            f"{resp.status_code} "
            f"{incident['disease']}"
        )

        return False

    except Exception as e:

        log.warning(f"[Submit] Failed: {e}")

        return False

# =============================================================================
# Main Pipeline
# =============================================================================

async def run_pipeline():

    log.info(
        "── CrisisWatch v3 pipeline starting ──"
    )

    async with httpx.AsyncClient(
        headers={
            "User-Agent":
            "CrisisWatch/3.0 "
            "(retrieval-grounded epidemic intelligence)"
        },
        follow_redirects=True
    ) as client:

        # ---------------------------------------------------------------------
        # Discovery
        # ---------------------------------------------------------------------

        rss_task = fetch_feed_candidates(client)
        gdelt_task = fetch_gdelt_candidates(client)
        climate_task = fetch_climate_triggers(client)

        rss_candidates, gdelt_candidates, climate_triggers = await asyncio.gather(
            rss_task,
            gdelt_task,
            climate_task,
            return_exceptions=True
        )

        candidates = []

        if isinstance(rss_candidates, list):
            candidates.extend(rss_candidates)

        if isinstance(gdelt_candidates, list):
            candidates.extend(gdelt_candidates)

        log.info(
            f"[Discovery] "
            f"{len(candidates)} candidates"
        )

        # ---------------------------------------------------------------------
        # Evidence Acquisition
        # ---------------------------------------------------------------------

        evidence_objects = []

        for candidate in candidates:

            evidence = await acquire_evidence(candidate)

            if evidence:
                evidence_objects.append(evidence)

            await asyncio.sleep(1)

        log.info(
            f"[Evidence] "
            f"{len(evidence_objects)} evidence packets"
        )

        # ---------------------------------------------------------------------
        # Gemini Synthesis
        # ---------------------------------------------------------------------

        synthesized_incidents = []

        for evidence in evidence_objects:

            synthesized = await synthesize_incident(
                client,
                evidence
            )

            incident = build_canonical_incident(
                synthesized,
                evidence,
                climate_triggers if isinstance(
                    climate_triggers,
                    dict
                ) else {}
            )

            if not incident:
                continue

            if incident["confidence"] < 0.35:
                continue

            synthesized_incidents.append(incident)

            # Gemini rate safety
            await asyncio.sleep(2)

        # ---------------------------------------------------------------------
        # Reconciliation
        # ---------------------------------------------------------------------

        synthesized_incidents = reconcile_signals(
            synthesized_incidents
        )

        log.info(
            f"[Reconciliation] "
            f"{len(synthesized_incidents)} incidents"
        )

        # ---------------------------------------------------------------------
        # Submission
        # ---------------------------------------------------------------------

        submitted = 0

        for incident in synthesized_incidents:

            ok = await submit_incident(
                client,
                incident.to_dict()
            )

            if ok:
                submitted += 1

        log.info(f"── Pipeline complete {success}/{len(synthesized_incidents)} (failed={failed}) ──")

# =============================================================================
# Scheduler
# =============================================================================

def run():
    asyncio.run(run_pipeline())

if __name__ == "__main__":

    scheduler = BlockingScheduler(
        timezone="UTC"
    )

    scheduler.add_job(
        run,
        "interval",
        minutes=30,
        next_run_time=datetime.now(
            timezone.utc
        )
    )

    log.info(
        "CrisisWatch v3 scheduler started "
        "— every 30 minutes"
    )

    try:
        scheduler.start()

    except KeyboardInterrupt:
        log.info("Pipeline stopped.")
