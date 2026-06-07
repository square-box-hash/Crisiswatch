import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Pipeline] %(message)s")
log = logging.getLogger(__name__)

CRISISWATCH_API = os.getenv("CRISISWATCH_API", "https://crisiswatch-ohrl.onrender.com")
SRAG_MCP_URL = os.getenv("SRAG_MCP_URL", "http://localhost:8000/mcp/sse")

# ── Sources ──────────────────────────────────────────────────────────────────

FEEDS = {
    "WHO DON": "https://www.who.int/rss-feeds/news-english.xml",
    "ProMED":  "https://promedmail.org/rss/feed.php",
    "ReliefWeb": "https://reliefweb.int/updates/rss.xml?primary_country=0&source=1600",
}

PUBMED_URL = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    "?db=pubmed&term=outbreak+OR+epidemic+OR+disease+cluster"
    "&sort=pub+date&retmax=10&retmode=json&datetype=pdat&reldate=2"
)

GDELT_URL = (
    "https://api.gdeltproject.org/api/v2/doc/doc"
    "?query=disease+outbreak+epidemic&mode=artlist&maxrecords=25"
    "&format=json&timespan=1d"
)

WHO_GHO_URL = (
    "https://ghoapi.azureedge.net/api/WHOSIS_000001"  # cause-of-death indicator
    "?$filter=TimeDim ge 2020&$top=50&$orderby=TimeDim desc"
)

CDC_SODA_URL = (
    "https://data.cdc.gov/resource/9mfq-cb36.json"
    "?$limit=20&$order=submission_date DESC"  # COVID surveillance as baseline
)

OPENMETEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=precipitation_sum,temperature_2m_max"
    "&forecast_days=7&timezone=auto"
)

# High-risk region climate coordinates for trigger correlation
CLIMATE_REGIONS = {
    "DRC":         (-4.0,  21.7),
    "Nigeria":     (9.0,   8.0),
    "Bangladesh":  (23.7,  90.4),
    "Ethiopia":    (9.0,   40.0),
    "Uganda":      (1.3,   32.3),
    "Kenya":       (0.0,   38.0),
    "India":       (20.6,  79.0),
    "Pakistan":    (30.4,  69.3),
    "Yemen":       (15.55, 48.5),
    "Haiti":       (18.97, -72.29),
}

# ── Deduplication ─────────────────────────────────────────────────────────────

_seen_hashes: set[str] = set()

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

def is_duplicate(title: str, source: str) -> bool:
    h = _hash(f"{source}:{title.lower().strip()}")
    if h in _seen_hashes:
        return True
    _seen_hashes.add(h)
    return False

# ── Disease signal extraction ─────────────────────────────────────────────────

DISEASE_KEYWORDS = {
    "ebola":    {"disease": "Ebola", "severity_hint": "critical"},
    "marburg":  {"disease": "Marburg", "severity_hint": "critical"},
    "cholera":  {"disease": "Cholera", "severity_hint": "moderate"},
    "mpox":     {"disease": "Mpox", "severity_hint": "high"},
    "monkeypox":{"disease": "Mpox", "severity_hint": "high"},
    "plague":   {"disease": "Plague", "severity_hint": "high"},
    "dengue":   {"disease": "Dengue", "severity_hint": "moderate"},
    "measles":  {"disease": "Measles", "severity_hint": "moderate"},
    "yellow fever": {"disease": "Yellow Fever", "severity_hint": "high"},
    "lassa":    {"disease": "Lassa Fever", "severity_hint": "high"},
    "nipah":    {"disease": "Nipah", "severity_hint": "critical"},
    "anthrax":  {"disease": "Anthrax", "severity_hint": "high"},
    "rift valley": {"disease": "Rift Valley Fever", "severity_hint": "high"},
    "meningitis": {"disease": "Meningitis", "severity_hint": "high"},
    "typhoid":  {"disease": "Typhoid", "severity_hint": "moderate"},
    "avian influenza": {"disease": "Avian Influenza", "severity_hint": "high"},
    "h5n1":     {"disease": "Avian Influenza H5N1", "severity_hint": "critical"},
}

COUNTRY_KEYWORDS = [
    "nigeria", "drc", "congo", "kenya", "ethiopia", "uganda", "somalia",
    "sudan", "cameroon", "chad", "mali", "niger", "guinea", "sierra leone",
    "liberia", "bangladesh", "india", "pakistan", "indonesia", "philippines",
    "haiti", "yemen", "mozambique", "tanzania", "angola", "zambia",
]

def extract_signal(title: str, summary: str, source: str) -> Optional[dict]:
    """Extract a structured outbreak signal from a news item"""
    text = f"{title} {summary}".lower()

    disease = None
    for keyword, meta in DISEASE_KEYWORDS.items():
        if keyword in text:
            disease = meta["disease"]
            break

    if not disease:
        # Check for generic outbreak language
        if not any(w in text for w in ["outbreak", "epidemic", "cluster", "cases reported", "surge"]):
            return None
        disease = "Unknown Pathogen"

    # Extract country
    country = "Unknown"
    region = "Unknown"
    for c in COUNTRY_KEYWORDS:
        if c in text:
            country = c.title()
            region = c.title()
            break

    # Rough case extraction
    import re
    cases_match = re.search(r"(\d+)\s+(?:confirmed\s+)?cases?", text)
    deaths_match = re.search(r"(\d+)\s+deaths?", text)
    cases = int(cases_match.group(1)) if cases_match else 10
    deaths = int(deaths_match.group(1)) if deaths_match else 0

    # Population lookup (rough)
    POP = {
        "Nigeria": 220000000, "Drc": 100000000, "Ethiopia": 120000000,
        "Kenya": 55000000, "Uganda": 48000000, "Bangladesh": 170000000,
        "India": 1400000000, "Pakistan": 230000000, "Haiti": 11000000,
        "Yemen": 34000000, "Guinea": 13000000, "Sierra Leone": 8000000,
    }
    population = POP.get(country, 5000000)

    return {
        "disease": disease,
        "region": region,
        "country": country,
        "cases": cases,
        "deaths": deaths,
        "population": population,
        "trigger": None,
        "source_urls": [source],
    }

# ── Fetchers ──────────────────────────────────────────────────────────────────

async def fetch_rss_signals(client: httpx.AsyncClient) -> list[dict]:
    signals = []
    for name, url in FEEDS.items():
        try:
            resp = await client.get(url, timeout=15)
            feed = feedparser.parse(resp.text)
            for entry in feed.entries[:10]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                if is_duplicate(title, name):
                    continue
                signal = extract_signal(title, summary, url)
                if signal:
                    log.info(f"[{name}] Found: {signal['disease']} in {signal['country']}")
                    signals.append(signal)
        except Exception as e:
            log.warning(f"[{name}] Feed error: {e}")
    return signals

async def fetch_gdelt_signals(client: httpx.AsyncClient) -> list[dict]:
    signals = []
    try:
        resp = await client.get(GDELT_URL, timeout=15)
        data = resp.json()
        articles = data.get("articles", [])
        for article in articles[:15]:
            title = article.get("title", "")
            url = article.get("url", "")
            if is_duplicate(title, "GDELT"):
                continue
            signal = extract_signal(title, "", url)
            if signal and signal["disease"] != "Unknown Pathogen":
                log.info(f"[GDELT] Found: {signal['disease']} in {signal['country']}")
                signals.append(signal)
    except Exception as e:
        log.warning(f"[GDELT] Error: {e}")
    return signals

async def fetch_climate_triggers(client: httpx.AsyncClient) -> dict[str, str]:
    """Return regions with active climate triggers (heavy rain → cholera, etc.)"""
    triggers = {}
    for region, (lat, lon) in CLIMATE_REGIONS.items():
        try:
            url = OPENMETEO_URL.format(lat=lat, lon=lon)
            resp = await client.get(url, timeout=10)
            data = resp.json()
            daily = data.get("daily", {})
            precip = daily.get("precipitation_sum", [0])
            max_precip = max(precip) if precip else 0
            if max_precip > 30:  # >30mm/day = heavy rain = cholera/flood trigger
                triggers[region] = "monsoon_flood"
                log.info(f"[Climate] Heavy rain trigger: {region} ({max_precip}mm)")
        except Exception as e:
            log.warning(f"[Climate] {region} error: {e}")
    return triggers

async def fetch_who_gho_signals(client: httpx.AsyncClient) -> list[dict]:
    """Fetch WHO Global Health Observatory case data"""
    signals = []
    try:
        resp = await client.get(WHO_GHO_URL, timeout=15)
        data = resp.json()
        for record in data.get("value", [])[:10]:
            country = record.get("SpatialDim", "Unknown")
            value = record.get("NumericValue", 0)
            year = record.get("TimeDim", 2024)
            if value and value > 1000 and year >= 2023:
                signals.append({
                    "disease": "Mortality Cluster",
                    "region": country,
                    "country": country,
                    "cases": int(value),
                    "deaths": int(value * 0.1),
                    "population": 5000000,
                    "trigger": None,
                    "source_urls": [WHO_GHO_URL],
                })
    except Exception as e:
        log.warning(f"[WHO GHO] Error: {e}")
    return signals

# ── SRag indexing ─────────────────────────────────────────────────────────────

async def index_to_srag(signal: dict, raw_text: str) -> None:
    """Index signal context into SRag for future retrieval"""
    try:
        from srag import SRag, SRagConfig
        config = SRagConfig(
            search_enabled=False,  # no web search, direct ingest
            rerank_enabled=True,
        )
        rag = SRag(config=config)
        await rag.ingest_text(
            text=raw_text,
            metadata={
                "source": "pipeline",
                "disease": signal["disease"],
                "country": signal["country"],
                "ingested_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.info(f"[SRag] Indexed: {signal['disease']} / {signal['country']}")
    except Exception as e:
        log.warning(f"[SRag] Index error: {e}")

# ── Main pipeline run ─────────────────────────────────────────────────────────

async def run_pipeline():
    log.info("── Pipeline run starting ──")
    
    async with httpx.AsyncClient(
        headers={"User-Agent": "CrisisWatch/1.0 (public health surveillance)"},
        follow_redirects=True
    ) as client:

        # Fetch from all sources concurrently
        rss_task      = fetch_rss_signals(client)
        gdelt_task    = fetch_gdelt_signals(client)
        climate_task  = fetch_climate_triggers(client)
        gho_task      = fetch_who_gho_signals(client)

        rss_signals, gdelt_signals, climate_triggers, gho_signals = await asyncio.gather(
            rss_task, gdelt_task, climate_task, gho_task,
            return_exceptions=True
        )

        # Flatten signals
        all_signals = []
        for result in [rss_signals, gdelt_signals, gho_signals]:
            if isinstance(result, list):
                all_signals.extend([
                    s for s in result
                    if s["disease"] != "Unknown Pathogen"   and s["country"] != "Unknown" # filter out generic signals
                ])

        if isinstance(climate_triggers, dict):
            # Apply climate triggers to signals
            for signal in all_signals:
                if signal["country"] in climate_triggers:
                    signal["trigger"] = climate_triggers[signal["country"]]

        log.info(f"Total new signals found: {len(all_signals)}")

        # Submit each signal to CrisisWatch API
        submitted = 0
        for signal in all_signals:
            try:
                resp = await client.post(
                    f"{CRISISWATCH_API}/outbreaks/detect",
                    json=signal,
                    timeout=60  # Gemini reasoning takes time
                )
                if resp.status_code == 200:
                    data = resp.json()
                    log.info(f"✓ Alert created: {data.get('outbreak_id')} — {signal['disease']} / {signal['country']}")
                    submitted += 1

                    # Index to SRag
                    raw_text = f"{signal['disease']} outbreak in {signal['region']}, {signal['country']}. Cases: {signal['cases']}, Deaths: {signal['deaths']}."
                    await index_to_srag(signal, raw_text)

                    # Rate limit — don't hammer Gemini
                    await asyncio.sleep(3)
                else:
                    log.warning(f"✗ API error {resp.status_code}: {signal['disease']}")
            except Exception as e:
                log.warning(f"✗ Submit error: {e}")

        log.info(f"── Pipeline run complete: {submitted}/{len(all_signals)} submitted ──")

def run():
    asyncio.run(run_pipeline())

if __name__ == "__main__":
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run, "interval", minutes=30, next_run_time=datetime.now(timezone.utc))
    log.info("CrisisWatch pipeline scheduler started — running every 30 minutes")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Pipeline stopped.")