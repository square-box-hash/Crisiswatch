import asyncio
import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from playwright.async_api import async_playwright

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

# =============================================================================
# CrisisWatch v4
# Direct Web Scrape → Gemini Extraction+Synthesis Pipeline
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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# =============================================================================
# Gemini Client
# =============================================================================

gemini_client = None

if GEMINI_API_KEY:
    try:
        from google import genai as _genai
        gemini_client = _genai.Client(api_key=GEMINI_API_KEY)
        log.info("[Gemini] Client initialized (google-genai SDK)")
    except ImportError:
        try:
            import google.generativeai as _genai
            _genai.configure(api_key=GEMINI_API_KEY)
            gemini_client = _genai
            log.info("[Gemini] Client initialized (google-generativeai SDK)")
        except ImportError:
            log.error("[Gemini] No SDK found. Run: pip install google-genai")
else:
    log.warning("[Gemini] GEMINI_API_KEY not set")

# =============================================================================
# Direct Web Sources
# These are full pages Gemini will read and extract outbreaks from directly.
# No RSS, no keyword pre-filtering — Gemini does all extraction.
# =============================================================================

SOURCES = {
    # WHO Disease Outbreak News listing page
    "WHO DON": {
        "url": "https://www.who.int/emergencies/disease-outbreak-news",
        "reputation": 0.98,
        "max_chars": 15000,
        "use_playwright": True,
    },

    # WHO AFRO outbreak bulletins
    "WHO AFRO": {
        "url": "https://www.afro.who.int/health-topics/disease-outbreaks/outbreaks-and-other-emergencies-updates",
        "reputation": 0.96,
    },

    # CDC International outbreak notices
    "CDC Travel Notices": {
        "url": "https://wwwnc.cdc.gov/travel/notices",
        "reputation": 0.95,
    },

    # HealthMap outbreak aggregator
    "HealthMap": {
        "url": "https://www.healthmap.org/en/",
        "reputation": 0.78,
    },

    # ReliefWeb disease/epidemic reports
    "ReliefWeb": {
        "url": "https://reliefweb.int/disasters?type=EP",
        "reputation": 0.92,
    },

    # Outbreak News Today
    "OutbreakNewsToday": {
        "url": "https://outbreaknewstoday.com/",
        "reputation": 0.72,
    },

    # ECDC threats page
    "ECDC": {
        "url": "https://www.ecdc.europa.eu/en/threats-and-outbreaks",
        "reputation": 0.94,
    },
}

# =============================================================================
# Open-Meteo Climate
# =============================================================================

OPENMETEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=precipitation_sum,temperature_2m_max"
    "&forecast_days=7&timezone=auto"
)

CLIMATE_REGIONS = {
    "DRC":        (-4.0,  21.7),
    "Nigeria":    ( 9.0,   8.0),
    "Bangladesh": (23.7,  90.4),
    "Ethiopia":   ( 9.0,  40.0),
    "Uganda":     ( 1.3,  32.3),
    "Kenya":      ( 0.0,  38.0),
    "India":      (20.6,  79.0),
    "Pakistan":   (30.4,  69.3),
    "Yemen":      (15.55, 48.5),
    "Haiti":      (18.97,-72.29),
}

# =============================================================================
# Reputation / Population
# =============================================================================

SOURCE_REPUTATION = {
    "WHO DON":          0.98,
    "WHO AFRO":         0.96,
    "CDC Travel Notices": 0.95,
    "ECDC":             0.94,
    "ReliefWeb":        0.92,
    "ProMED":           0.90,
    "HealthMap":        0.78,
    "OutbreakNewsToday": 0.72,
    "GDELT":            0.42,
    "Unknown":          0.30,
}

POP = {
    "Nigeria":    220_000_000,
    "DRC":        100_000_000,
    "Ethiopia":   120_000_000,
    "Kenya":       55_000_000,
    "Uganda":      48_000_000,
    "Bangladesh": 170_000_000,
    "India":    1_400_000_000,
    "Pakistan":   230_000_000,
    "Haiti":       11_000_000,
    "Yemen":       34_000_000,
}

# =============================================================================
# Runtime dedup
# =============================================================================

_seen_hashes: set[str] = set()

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def is_duplicate(disease: str, country: str) -> bool:
    h = _hash(f"{disease.lower()}:{country.lower()}:{datetime.now(timezone.utc).date()}")
    if h in _seen_hashes:
        return True
    _seen_hashes.add(h)
    return False

# =============================================================================
# HTML → clean text
# No external deps — strip tags with regex, collapse whitespace
# =============================================================================

def html_to_text(html: str, max_chars: int = 12_000) -> str:
    # Remove scripts and style blocks entirely
    text = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove all remaining HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode common HTML entities
    text = (text
        .replace('&amp;',  '&')
        .replace('&lt;',   '<')
        .replace('&gt;',   '>')
        .replace('&nbsp;', ' ')
        .replace('&#39;',  "'")
        .replace('&quot;', '"')
    )
    # Collapse whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    # Truncate to avoid Gemini context overflow
    return text[:max_chars]

# =============================================================================
# Web Scraper
# =============================================================================

async def scrape_with_playwright(url: str, max_chars: int = 12000) -> Optional[str]:
    """For JS-rendered pages that httpx can't read properly."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (compatible; CrisisWatch/4.0)"
            )
            await page.goto(url, timeout=30000, wait_until="networkidle")
            html = await page.content()
            await browser.close()
            return html_to_text(html, max_chars)
    except Exception as e:
        log.warning(f"[Playwright] {url} failed: {e}")
        return None

async def scrape_source(
    client: httpx.AsyncClient,
    source_name: str,
    source_cfg: dict,
) -> Optional[dict]:
    """Fetch a source URL and return clean text. Falls back to Playwright for JS-rendered pages."""

    url = source_cfg["url"]
    max_chars = source_cfg.get("max_chars", 12000)
    clean = ""

    # ── Try fast httpx first ──
    try:
        resp = await client.get(url, timeout=30)

        if resp.status_code == 200:
            clean = html_to_text(resp.text, max_chars)
        else:
            log.warning(f"[Scrape] {source_name} → HTTP {resp.status_code}")

    except Exception as e:
        log.warning(f"[Scrape] {source_name} httpx failed: {e}")

    # ── Fallback to Playwright if httpx result is too short or source needs JS ──
    if len(clean) < 500 or source_cfg.get("use_playwright"):
        log.info(f"[Scrape] {source_name} → falling back to Playwright")
        clean = await scrape_with_playwright(url, max_chars)

    if not clean or len(clean) < 200:
        log.warning(f"[Scrape] {source_name} → too short, skipping")
        return None

    log.info(f"[Scrape] {source_name} → {len(clean)} chars extracted")

    return {
        "source_name": source_name,
        "url": url,
        "reputation": source_cfg["reputation"],
        "text": clean,
    }
# =============================================================================
# Gemini Extraction + Synthesis (one prompt does both)
# =============================================================================


async def extract_outbreaks_from_source(
    source: dict,
) -> list[dict]:
    """Send scraped text to Gemini, get back list of outbreak dicts."""

    if gemini_client is None:
        log.warning("[Gemini] Client not ready, skipping extraction")
        return []

    log.info(f"[Debug] {source['source_name']} scraped text:\n{source['text'][:800]}")

    raw_text = ""
    try:

        tier = "HIGH" if source["source_name"] in ["WHO DON", "WHO AFRO", "CDC Travel Notices"] else "MEDIUM"
        prompt = (EXTRACTION_PROMPT
            .replace("{source_name}", source["source_name"])
            .replace("{url}",         source["url"])
            .replace("{page_text}",   source["text"])
            .replace("{source_reputation_tier}",  tier)
        )

        import time

        def _call_gemini():
            last_exc = None
            for attempt in range(3):
                try:
                    return gemini_client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=prompt,
                    )
                except Exception as exc:
                    last_exc = exc
                    if "503" in str(exc) or "UNAVAILABLE" in str(exc):
                        wait = 5 * (attempt + 1)
                        log.info(f"[Gemini] 503 — retrying in {wait}s (attempt {attempt+1}/3)...")
                        time.sleep(wait)
                    else:
                        raise
            raise last_exc

        response = await asyncio.to_thread(_call_gemini)

        # Extract text — handle both SDK response shapes
        if hasattr(response, "text"):
            raw_text = response.text or ""
        elif hasattr(response, "candidates"):
            raw_text = response.candidates[0].content.parts[0].text or ""
        else:
            log.warning(f"[Gemini] {source['source_name']} → unrecognised response shape: {type(response)}")
            return []

        raw_text = raw_text.strip()
        log.debug(f"[Gemini] {source['source_name']} raw: {raw_text[:300]}")

        # Strip markdown fences
        text = re.sub(r'^```json\s*', '', raw_text, flags=re.MULTILINE)
        text = re.sub(r'^```\s*',     '', text,     flags=re.MULTILINE)
        text = re.sub(r'```\s*$',     '', text,     flags=re.MULTILINE)
        text = text.strip()

        outbreaks = json.loads(text)

        # Gemini sometimes returns a single object instead of an array
        if isinstance(outbreaks, dict):
            log.info(f"[Gemini] {source['source_name']} → single object, wrapping in list")
            outbreaks = [outbreaks]

        if not isinstance(outbreaks, list):
            log.warning(f"[Gemini] {source['source_name']} → unexpected type {type(outbreaks)}")
            return []

        log.info(f"[Gemini] {source['source_name']} → {len(outbreaks)} outbreak(s) extracted")

        for ob in outbreaks:
            ob["source_name"] = source["source_name"]
            ob["source_url"]  = source["url"]
            ob["reputation"]  = source["reputation"]

        return outbreaks

    except json.JSONDecodeError as e:
        log.warning(f"[Gemini] {source['source_name']} → JSON parse error: {e}")
        log.warning(f"[Gemini] Raw was: {raw_text[:500]}")
        return []

    except Exception as e:
        import traceback
        log.warning(f"[Gemini] {source['source_name']} → exception: {type(e).__name__}: {e}")
        log.warning(traceback.format_exc())
        return []


EXTRACTION_PROMPT = """
You are a senior epidemiological intelligence analyst at a global health surveillance organization. You have 20 years of experience reading WHO, CDC, ECDC, and ReliefWeb reports and you brief government health ministers daily.

Your task: Read the following web page and extract all active disease outbreaks or public health emergencies into structured JSON.

═══════════════════════════════════════
REASONING PROCESS — follow this exactly
═══════════════════════════════════════

Step 1 — SCAN: Read the entire page. List every disease, location, and number you see.
Step 2 — FILTER: Remove anything that is historical, resolved, preparedness-only, vaccination campaigns, or animal-only events.
Step 3 — DEDUPLICATE: If the same disease appears in multiple countries, create ONE entry for the most affected country. List others in neighboring_mentions.
Step 4 — EXTRACT NUMBERS: Pull exact human case/death counts. If the page says "635 confirmed cases" use 635. If it says "dozens" use 40. If truly unknown use 0.
Step 5 — CLASSIFY: Assign lifecycle stage, spread risk, confidence and claim type using the rules below.
Step 6 — SUMMARIZE: Write a 2-3 sentence summary as if briefing a health minister. Be specific, not vague.

═══════════════════════════════════════
CRITICAL RULES — never violate these
═══════════════════════════════════════

HUMAN CASES ONLY:
- H5N1 in dairy cattle, poultry flocks, or wild birds = NOT human cases
- "1,000 herds affected" + "71 human cases" → estimated_cases: 71
- Animal detections are background context only, never the case count

EXACT NUMBER EXTRACTION:
- "635 confirmed cases" → 635
- "two deaths" → 2
- "dozens of cases" → 40
- "hundreds affected" → 200
- "cases reported" (no number) → 0
- Never return 0 if a number is explicitly stated anywhere on the page

DISEASE NAME NORMALIZATION:
- Bundibugyo virus / Bundibugyo virus disease → "Ebola (Bundibugyo)"
- Monkeypox / Mpox / Clade I / Clade II → "Mpox"
- Avian influenza / H5N1 / bird flu → "H5N1 Avian Influenza"
- COVID-19 / SARS-CoV-2 → "COVID-19"
- Always use the most widely recognized name

ONE ENTRY PER OUTBREAK:
- H5N1 in US + UK + Israel = ONE entry, primary_country = most cases, others in neighboring_mentions
- Same disease in neighboring countries during same outbreak = ONE entry
- Only create separate entries if outbreaks are clearly epidemiologically unrelated

CONFIDENCE CALIBRATION:
- WHO DON official report with case counts: 0.90-0.98
- CDC/ECDC official advisory: 0.85-0.95
- ReliefWeb situation report: 0.80-0.92
- News article with named sources: 0.65-0.78
- Vague mention, no source cited: 0.30-0.50
- Multiply by 0.9 if no case count available
- Never give 0.95+ to a vague mention

LIFECYCLE STAGE:
- emerging: first reports, unconfirmed, investigation ongoing
- suspected: cases reported but lab confirmation pending
- confirmed: lab confirmed, limited spread
- active: confirmed, ongoing transmission, response underway
- contained: cases declining, response controlling spread
- resolved: no new cases for 2+ incubation periods
- historical: past outbreak being referenced

SPREAD RISK:
- low: localized, contained, no cross-border risk
- moderate: some spread potential, neighboring countries at risk
- high: multi-country spread already occurring or imminent
- critical: pandemic potential, global spread risk

═══════════════════════════════════════
EXAMPLES — learn from these
═══════════════════════════════════════

EXAMPLE 1 — Correct human vs animal case extraction:
Page text: "H5N1 has affected over 1,000 dairy herds across 17 US states. As of May 2026, 71 human cases have been confirmed since 2024, primarily in farmworkers."
Correct output:
{
  "disease": "H5N1 Avian Influenza",
  "primary_country": "United States",
  "estimated_cases": 71,
  "estimated_deaths": 2,
  "spread_risk": "moderate",
  "confidence": 0.88,
  "reasoning": "71 human cases explicitly stated. 1,000 herds is animal data, ignored for human case count."
}

EXAMPLE 2 — Correct number extraction from partial text:
Page text: "The Ebola outbreak in DRC caused by Bundibugyo virus has now reached 635 confirmed cases including 127 deaths. Uganda has reported imported cases."
Correct output:
{
  "disease": "Ebola (Bundibugyo)",
  "primary_country": "Democratic Republic of the Congo",
  "neighboring_mentions": ["Uganda"],
  "estimated_cases": 635,
  "estimated_deaths": 127,
  "spread_risk": "high",
  "confidence": 0.95,
  "reasoning": "635 confirmed cases and 127 deaths explicitly stated. Uganda mentioned as having imported cases so listed in neighboring_mentions, not as separate entry."
}

EXAMPLE 3 — Correct deduplication:
Page text: "H5N1 has been detected in the UK, Israel, and India. The UK has 3 confirmed human cases. Israel has 2. India has 1."
Correct output:
{
  "disease": "H5N1 Avian Influenza",
  "primary_country": "United Kingdom",
  "neighboring_mentions": ["Israel", "India"],
  "estimated_cases": 6,
  "spread_risk": "moderate",
  "confidence": 0.85,
  "reasoning": "Same global H5N1 situation affecting multiple countries. UK has most cases so is primary. Total human cases summed: 3+2+1=6."
}

EXAMPLE 4 — Correct handling of vague text with no numbers:
Page text: "Cholera cases have been reported in Zambia following flooding."
Correct output:
{
  "disease": "Cholera",
  "primary_country": "Zambia",
  "estimated_cases": 0,
  "spread_risk": "moderate",
  "confidence": 0.62,
  "lifecycle_stage": "emerging",
  "reasoning": "Cases reported but no specific count given. Confidence reduced due to lack of numbers. Flooding context suggests moderate spread risk."
}

EXAMPLE 5 — What NOT to include:
Page text: "WHO recommends countries prepare for potential Marburg outbreak. No cases currently reported."
Correct output: [] — this is preparedness, not an active outbreak.

═══════════════════════════════════════
SOURCE CONTEXT
═══════════════════════════════════════
Source: {source_name}
URL: {url}
Reputation tier: {source_reputation_tier}

Page content:
{page_text}

═══════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════
Return ONLY a valid JSON array. No markdown, no preamble, no explanation outside the JSON.
If nothing qualifies as an active outbreak, return exactly: []

Each item must follow this exact schema:
{
  "outbreak_detected": true,
  "disease": "canonical disease name",
  "primary_country": "most affected country",
  "neighboring_mentions": ["other countries mentioned"],
  "claim_type": "official | media | rumor | unknown",
  "lifecycle_stage": "emerging | suspected | confirmed | active | contained | resolved | historical",
  "confidence": 0.0 to 1.0,
  "estimated_cases": integer human cases only,
  "estimated_deaths": integer human deaths only,
  "spread_risk": "low | moderate | high | critical",
  "summary": "2-3 sentence briefing for a government health minister — specific, factual, no vague language",
  "reasoning": "step by step explanation referencing specific text from the page"
}
"""

# =============================================================================
# Climate Layer  (fully optional — never blocks the pipeline)
# =============================================================================

async def fetch_climate_triggers(
    client: httpx.AsyncClient,
) -> dict[str, str]:

    triggers = {}

    async def _fetch_one(region: str, lat: float, lon: float) -> None:
        try:
            url  = OPENMETEO_URL.format(lat=lat, lon=lon)
            resp = await client.get(url, timeout=5)  # short — optional data
            if resp.status_code != 200:
                return
            daily  = resp.json().get("daily", {})
            precip = daily.get("precipitation_sum", [0])
            max_p  = max(precip) if precip else 0
            if max_p > 30:
                triggers[region] = "monsoon_flood"
                log.info(f"[Climate] {region} trigger ({max_p:.1f}mm)")
        except Exception:
            pass  # silently skip — climate enrichment is optional

    # All regions fire concurrently; failures are silently ignored
    await asyncio.gather(
        *[_fetch_one(r, lat, lon) for r, (lat, lon) in CLIMATE_REGIONS.items()],
        return_exceptions=True,
    )

    log.info(
        f"[Climate] Done — {len(triggers)} trigger(s) detected"
        if triggers else
        "[Climate] No triggers (or service unreachable) — continuing"
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
    VerificationStatus,
)

def derive_severity(spread_risk, lifecycle, cases, deaths, population):
    population = population or 10_000_000

    cases_per_million = (cases / population) * 1_000_000
    deaths_per_million = (deaths / population) * 1_000_000

    if spread_risk == SpreadRisk.EXTREME or deaths_per_million > 5:
        return SeverityLevel.CRITICAL

    if (
        spread_risk == SpreadRisk.HIGH
        or cases_per_million > 50
        or deaths_per_million > 1
        or (lifecycle == OutbreakLifecycle.ACTIVE and deaths > 10)
    ):
        return SeverityLevel.HIGH

    if spread_risk == SpreadRisk.MODERATE or cases_per_million > 5 or cases > 20:
        return SeverityLevel.MODERATE

    return SeverityLevel.LOW

def build_canonical_incident(
    outbreak: dict,
    climate_triggers: dict[str, str],
) -> Optional[CanonicalIncident]:

    if not outbreak.get("outbreak_detected"):
        return None

    disease = outbreak.get("disease", "").strip()
    country = outbreak.get("primary_country", "").strip()

    if not disease or not country:
        return None

    if is_duplicate(disease, country):
        log.info(f"[Dedup] Skipping {disease} / {country}")
        return None

    # Confidence calibration
    raw_conf   = float(outbreak.get("confidence", 0.5))
    reputation = float(outbreak.get("reputation", 0.5))
    confidence = round(min(max(raw_conf * reputation, 0.05), 0.99), 3)

    incident_id = _hash(
        f"{disease}:{country}:{datetime.now(timezone.utc).date()}"
    )

    # Enum-safe mappings
    try:
        lifecycle = OutbreakLifecycle(outbreak.get("lifecycle_stage", "active"))
    except Exception:
        lifecycle = OutbreakLifecycle.ACTIVE

    try:
        claim_type = ClaimType(outbreak.get("claim_type", "official"))
    except Exception:
        claim_type = ClaimType.OFFICIAL

    try:
        spread_risk = SpreadRisk(outbreak.get("spread_risk", "moderate"))
    except Exception:
        spread_risk = SpreadRisk.MODERATE

    population = POP.get(country, 10_000_000)
    severity = derive_severity(
        spread_risk,
        lifecycle,
        int(outbreak.get("estimated_cases", 0)),
        int(outbreak.get("estimated_deaths", 0)),
        population,
    )

    return CanonicalIncident(
        incident_id   = incident_id,
        disease       = disease,
        region        = country,
        country       = country,
        population    = population,

        estimated_cases  = int(outbreak.get("estimated_cases",  0)),
        estimated_deaths = int(outbreak.get("estimated_deaths", 0)),

        severity          = severity,
        spread_risk       = spread_risk,
        lifecycle_stage   = lifecycle,
        claim_type        = claim_type,
        confidence_score  = confidence,
        verification_status = VerificationStatus.VERIFIED,

        trigger       = climate_triggers.get(country),
        source_count  = 1,
        source_urls   = [outbreak.get("source_url", "")],

        neighboring_mentions = outbreak.get("neighboring_mentions", []),
        ai_summary           = outbreak.get("summary",   ""),
        reasoning            = outbreak.get("reasoning", ""),
        evidence_session     = None,
        metadata             = {},
    )

# =============================================================================
# Reconciliation — merge duplicate disease/country pairs across sources
# =============================================================================

def reconcile_signals(
    incidents: list[CanonicalIncident],
) -> list[CanonicalIncident]:

    grouped = defaultdict(list)

    for inc in incidents:
        key = (inc.disease.lower(), inc.country.lower())
        grouped[key].append(inc)

    merged = []

    for _, group in grouped.items():

        group.sort(key=lambda x: x.confidence_score, reverse=True)
        primary = group[0]

        if len(group) > 1:
            # Boost confidence slightly for corroboration
            primary.confidence_score = round(
                min(primary.confidence_score + 0.05 * (len(group) - 1), 0.99),
                3,
            )
            # Merge all source URLs
            primary.source_urls  = list({u for g in group for u in g.source_urls})
            primary.source_count = len(primary.source_urls)

        merged.append(primary)

    return merged

# =============================================================================
# Submission
# =============================================================================

async def submit_incident(
    client: httpx.AsyncClient,
    incident: dict,
) -> bool:

    try:
        # Convert any datetime objects to ISO strings before JSON serialization
        def _serialize(obj):
            if hasattr(obj, 'isoformat'):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        import json as _json
        incident_json = _json.loads(_json.dumps(incident, default=_serialize))

        resp = await client.post(
            f"{CRISISWATCH_API}/outbreaks/detect",
            json=incident_json,
            timeout=90,
        )

        if resp.status_code == 200:
            log.info(
                f"✓ Submitted {incident['disease']} / {incident['country']} "
                f"(conf={incident['confidence_score']})"
            )
            return True

        log.warning(f"[Submit] HTTP {resp.status_code} for {incident['disease']}")
        return False

    except Exception as e:
        log.warning(f"[Submit] Failed: {e}")
        return False

# =============================================================================
# Main Pipeline
# =============================================================================

async def run_pipeline():

    log.info("── CrisisWatch v4 pipeline starting ──")

    async with httpx.AsyncClient(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; "
                "CrisisWatch/4.0; "
                "+https://github.com/square-box-hash/Crisiswatch)"
            )
        },
        follow_redirects=True,
    ) as client:

        # ── 1. Scrape all sources concurrently ──
        log.info(f"[Scrape] Fetching {len(SOURCES)} sources...")

        scrape_tasks = [
            scrape_source(client, name, cfg)
            for name, cfg in SOURCES.items()
        ]

        scraped_results = await asyncio.gather(
            *scrape_tasks,
            return_exceptions=True,
        )

        scraped = [
            r for r in scraped_results
            if isinstance(r, dict)
        ]

        log.info(f"[Scrape] {len(scraped)}/{len(SOURCES)} sources fetched successfully")

        # ── 2. Climate triggers (concurrently with scraping above, but done by now) ──
        climate_triggers = await fetch_climate_triggers(client)

        # ── 3. Gemini extraction — one call per source, with rate spacing ──
        all_outbreaks: list[dict] = []

        for source in scraped:
            outbreaks = await extract_outbreaks_from_source(source)
            all_outbreaks.extend(outbreaks)
            await asyncio.sleep(2)  # Gemini rate spacing

        log.info(f"[Extraction] {len(all_outbreaks)} total outbreaks extracted")

        # ── 4. Build canonical incidents ──
        incidents: list[CanonicalIncident] = []

        for outbreak in all_outbreaks:
            incident = build_canonical_incident(outbreak, climate_triggers)
            if incident is None:
                continue
            if incident.confidence_score < 0.35:
                log.info(
                    f"[Filter] Low confidence dropped: "
                    f"{incident.disease}/{incident.country} "
                    f"({incident.confidence_score})"
                )
                continue
            incidents.append(incident)

        # ── 5. Reconcile duplicates across sources ──
        incidents = reconcile_signals(incidents)
        log.info(f"[Reconciliation] {len(incidents)} unique incidents")

        # ── 6. Submit ──
        submitted = 0

        for incident in incidents:
            ok = await submit_incident(client, incident.to_dict())
            if ok:
                submitted += 1

        log.info(
            f"── Pipeline complete: "
            f"{submitted}/{len(incidents)} submitted ──"
        )

# =============================================================================
# Scheduler
# =============================================================================

def run():
    asyncio.run(run_pipeline())

if __name__ == "__main__":

    import argparse
    parser = argparse.ArgumentParser(description="CrisisWatch v4 pipeline")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the pipeline once and exit (no scheduler)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Scheduler interval in minutes (default: 30)",
    )
    args = parser.parse_args()

    if args.once:
        log.info("CrisisWatch v4 — running once")
        run()
        log.info("Done.")
    else:
        scheduler = BlockingScheduler(timezone="UTC")
        scheduler.add_job(
            run,
            "interval",
            minutes=args.interval,
            next_run_time=datetime.now(timezone.utc),
        )
        log.info(f"CrisisWatch v4 scheduler started — every {args.interval} minutes")
        log.info("Press Ctrl+C to stop")
        try:
            scheduler.start()
        except KeyboardInterrupt:
            log.info("Pipeline stopped.")