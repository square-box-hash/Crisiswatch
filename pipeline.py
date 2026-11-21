"""
CrisisWatch v5 — live ingestion.

    sources (RSS / API / listing)  →  article pages  →  Gemini extraction
                                   →  canonical incidents  →  MongoDB upsert

Every stage records what it did into `pipeline_runs`, so a run that produces
nothing says why instead of silently returning an empty list.

    python pipeline.py --once            # one run
    python pipeline.py --once --dry-run  # extract, print, write nothing
    python pipeline.py --interval 30     # run every 30 minutes
"""

import argparse
import asyncio
import json
import logging
import os
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

import db
import sources
import store
from extract import extract_from_candidate, init_client
from fetch import fetch_page, gather_limited, make_client
from models import (
    CanonicalIncident,
    OutbreakLifecycle,
    SeverityLevel,
    SpreadRisk,
    VerificationStatus,
    coerce_claim_type,
    coerce_lifecycle,
    coerce_spread_risk,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("crisiswatch")

MAX_ARTICLES = int(os.getenv("MAX_ARTICLES_PER_RUN", "24"))
FETCH_CONCURRENCY = int(os.getenv("FETCH_CONCURRENCY", "5"))
GEMINI_CONCURRENCY = int(os.getenv("GEMINI_CONCURRENCY", "2"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.30"))

OPENMETEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=precipitation_sum&forecast_days=7&timezone=auto"
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

POP = {
    "Nigeria":    220_000_000,
    "DRC":        100_000_000,
    "Democratic Republic of the Congo": 100_000_000,
    "Ethiopia":   120_000_000,
    "Kenya":       55_000_000,
    "Uganda":      48_000_000,
    "Bangladesh": 170_000_000,
    "India":    1_400_000_000,
    "Pakistan":   230_000_000,
    "Haiti":       11_000_000,
    "Yemen":       34_000_000,
    "United States": 335_000_000,
    "Brazil":     215_000_000,
    "Indonesia":  280_000_000,
    "Philippines": 115_000_000,
    "Zambia":      20_000_000,
}


# =============================================================================
# Climate layer — optional, never blocks a run
# =============================================================================

async def fetch_climate_triggers(client) -> dict:
    triggers = {}

    async def _one(region: str, lat: float, lon: float) -> None:
        try:
            resp = await client.get(
                OPENMETEO_URL.format(lat=lat, lon=lon), timeout=8
            )
            if resp.status_code != 200:
                return
            daily = resp.json().get("daily", {})
            precip = daily.get("precipitation_sum") or [0]
            peak = max([p for p in precip if p is not None] or [0])
            if peak > 30:
                triggers[region] = "monsoon_flood"
        except Exception:
            return

    await gather_limited(
        [_one(r, lat, lon) for r, (lat, lon) in CLIMATE_REGIONS.items()], limit=6
    )

    log.info(f"[Climate] {len(triggers)} trigger(s)")
    return triggers


# =============================================================================
# Severity
# =============================================================================

def derive_severity(
    spread_risk: SpreadRisk,
    lifecycle: OutbreakLifecycle,
    cases: int,
    deaths: int,
    population: int,
) -> SeverityLevel:

    population = population or 10_000_000
    cases_per_million = (cases / population) * 1_000_000
    deaths_per_million = (deaths / population) * 1_000_000

    if spread_risk == SpreadRisk.CRITICAL or deaths_per_million > 5:
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


def calibrate_confidence(raw: float, reputation: float) -> float:
    """
    Temper the model's self-reported confidence with source reputation.
    A blend rather than a product: multiplying made every news source look
    like a rumour (0.90 × 0.72 = 0.65) and pushed real outbreaks under the
    drop threshold.
    """

    factor = 0.6 + 0.4 * max(0.0, min(1.0, reputation))
    return round(max(0.05, min(raw * factor, 0.99)), 3)


# =============================================================================
# Incident builder
# =============================================================================

def build_incident(
    item: dict,
    candidate,
    climate_triggers: dict,
    now: Optional[datetime] = None,
) -> Optional[CanonicalIncident]:

    now = now or datetime.now(timezone.utc)

    disease = str(item.get("disease", "")).strip()
    country = str(item.get("primary_country", "")).strip()

    if not disease or not country:
        return None

    cases = int(item.get("estimated_cases") or 0)
    deaths = int(item.get("estimated_deaths") or 0)

    lifecycle = coerce_lifecycle(item.get("lifecycle_stage"))
    claim_type = coerce_claim_type(item.get("claim_type"))
    spread_risk = coerce_spread_risk(item.get("spread_risk"))

    population = POP.get(country, 10_000_000)

    confidence = calibrate_confidence(
        float(item.get("confidence", 0.5)), candidate.reputation
    )

    severity = derive_severity(spread_risk, lifecycle, cases, deaths, population)

    key = store.incident_key(disease, country)

    return CanonicalIncident(
        incident_id=key,
        incident_key=key,
        disease=disease,
        region=country,
        country=country,
        population=population,
        estimated_cases=cases,
        estimated_deaths=deaths,
        severity=severity,
        spread_risk=spread_risk,
        lifecycle_stage=lifecycle,
        claim_type=claim_type,
        confidence_score=confidence,
        verification_status=VerificationStatus.UNVERIFIED,
        trigger=climate_triggers.get(country),
        source_count=1,
        source_urls=[candidate.url],
        source_names=[candidate.source_name],
        neighboring_mentions=[
            str(c).strip() for c in (item.get("neighboring_mentions") or []) if c
        ],
        ai_summary=str(item.get("summary", "")).strip(),
        reasoning=str(item.get("reasoning", "")).strip(),
        published_at=candidate.published_at,
        metadata={"title": candidate.title, "tier": candidate.tier},
        created_at=now,
        updated_at=now,
    )


def reconcile(incidents: list) -> list:
    """Merge the same outbreak seen by several sources."""

    grouped = defaultdict(list)

    for inc in incidents:
        grouped[inc.incident_key].append(inc)

    merged = []

    for _, group in grouped.items():
        group.sort(key=lambda x: x.confidence_score, reverse=True)
        primary = group[0]

        if len(group) > 1:
            primary.confidence_score = round(
                min(primary.confidence_score + 0.05 * (len(group) - 1), 0.99), 3
            )
            primary.source_urls = list(
                {u for g in group for u in g.source_urls if u}
            )
            primary.source_names = list(
                {n for g in group for n in g.source_names if n}
            )
            primary.source_count = len(primary.source_urls)

            # Take the highest case count reported for this outbreak.
            primary.estimated_cases = max(g.estimated_cases for g in group)
            primary.estimated_deaths = max(g.estimated_deaths for g in group)

            primary.neighboring_mentions = list(
                {c for g in group for c in g.neighboring_mentions}
            )

            primary.severity = derive_severity(
                primary.spread_risk,
                primary.lifecycle_stage,
                primary.estimated_cases,
                primary.estimated_deaths,
                primary.population,
            )

        merged.append(primary)

    return merged


# =============================================================================
# Pipeline
# =============================================================================

async def _fetch_article_text(client, candidate, max_chars: int = 10_000) -> str:
    """Use inline text (ReliefWeb body, RSS summary) when it's substantial."""

    if candidate.inline_text and len(candidate.inline_text) > 800:
        return candidate.inline_text

    text = await fetch_page(client, candidate.url, max_chars=max_chars)

    if not text and candidate.inline_text:
        return candidate.inline_text

    return text or ""


async def run_pipeline(
    dry_run: bool = False,
    max_articles: Optional[int] = None,
) -> dict:
    """One full run. Returns a summary dict, also written to `pipeline_runs`."""

    started = datetime.now(timezone.utc)
    limit = max_articles or MAX_ARTICLES

    run_id = None if dry_run else store.start_run()

    summary = {
        "status": "running",
        "sources": {},
        "candidates": 0,
        "articles_fetched": 0,
        "items_extracted": 0,
        "incidents_created": 0,
        "incidents_updated": 0,
        "errors": [],
    }

    log.info(f"── CrisisWatch run starting (limit {limit} articles) ──")

    client = None

    try:
        if not dry_run:
            # Clear out runs left "running" by an instance that died mid-run.
            stale = store.mark_stale_runs()
            if stale:
                log.info(f"[Runs] marked {stale} stale run(s) as abandoned")

            db.ensure_indexes()

            if init_client() is None:
                summary["errors"].append(
                    "Gemini client unavailable — set GEMINI_API_KEY"
                )
                summary["status"] = "failed"
                return summary

        client = make_client()

        # ── 1. Collect candidate articles from every source ──
        collected = await asyncio.gather(
            *[
                sources.collect_source(client, name, cfg)
                for name, cfg in sources.SOURCES.items()
            ],
            return_exceptions=True,
        )

        candidates = []
        seen = set()

        for (name, _cfg), result in zip(sources.SOURCES.items(), collected):
            if isinstance(result, Exception):
                summary["errors"].append(f"{name}: {type(result).__name__}: {result}")
                summary["sources"][name] = 0
                continue
            summary["sources"][name] = len(result)
            for c in result:
                if c.key() in seen:
                    continue
                seen.add(c.key())
                candidates.append(c)

        summary["candidates"] = len(candidates)
        log.info(f"[Collect] {len(candidates)} unique candidate(s) from {len(sources.SOURCES)} source(s)")

        if not candidates:
            summary["status"] = "no_sources"
            summary["errors"].append(
                "No candidates from any source — every feed returned nothing. "
                "Check the [Source] log lines above."
            )
            store.finish_run(run_id, summary)
            return summary

        # Newest first, then cap.
        candidates.sort(
            key=lambda c: c.published_at or datetime(1970, 1, 1, tzinfo=timezone.utc),
            reverse=True,
        )
        candidates = candidates[:limit]

        # ── 2. Fetch article text ──
        texts = await gather_limited(
            [_fetch_article_text(client, c) for c in candidates],
            limit=FETCH_CONCURRENCY,
        )

        articles = []
        for candidate, text in zip(candidates, texts):
            if not text:
                summary["errors"].append(f"fetch failed: {candidate.url}")
                continue
            candidate.text = text
            articles.append(candidate)

        summary["articles_fetched"] = len(articles)
        log.info(f"[Fetch] {len(articles)}/{len(candidates)} article(s) had usable text")

        if not articles:
            summary["status"] = "no_articles"
            summary["errors"].append("Candidates found but no article text could be fetched")
            store.finish_run(run_id, summary)
            return summary

        # ── 3. Climate (optional) ──
        climate = await fetch_climate_triggers(client)

        # ── 4. Gemini extraction ──
        results = await gather_limited(
            [extract_from_candidate(a) for a in articles],
            limit=GEMINI_CONCURRENCY,
        )

        incidents = []

        for candidate, result in zip(articles, results):
            if not result:
                continue
            items, error = result
            if error:
                summary["errors"].append(f"{candidate.source_name}: {error}")
                continue
            summary["items_extracted"] += len(items)
            for item in items:
                incident = build_incident(item, candidate, climate, now=started)
                if incident is None:
                    continue
                if incident.confidence_score < MIN_CONFIDENCE:
                    log.info(
                        f"[Filter] {incident.disease}/{incident.country} "
                        f"confidence {incident.confidence_score} < {MIN_CONFIDENCE}"
                    )
                    continue
                incidents.append(incident)

        log.info(f"[Extract] {summary['items_extracted']} item(s) → {len(incidents)} incident(s)")

        # ── 5. Merge across sources ──
        incidents = reconcile(incidents)
        log.info(f"[Reconcile] {len(incidents)} unique incident(s)")

        # ── 6. Persist ──
        for incident in incidents:
            doc = incident.to_doc()
            if dry_run:
                summary["incidents_created"] += 1
                print(json.dumps(doc, default=str, indent=2)[:1200])
                continue
            try:
                result = store.upsert_incident(doc, now=started)
            except Exception as exc:
                summary["errors"].append(f"store: {type(exc).__name__}: {exc}")
                continue
            if result["created"]:
                summary["incidents_created"] += 1
            else:
                summary["incidents_updated"] += 1

        summary["status"] = "ok" if incidents else "no_outbreaks"
        if not incidents:
            summary["errors"].append(
                "Sources and Gemini both worked, but nothing qualified as an "
                "active outbreak (or every item fell below MIN_CONFIDENCE)."
            )

    except Exception as exc:
        # A run that dies must say so, not sit at "running" forever.
        summary["status"] = "failed"
        summary["errors"].append(f"{type(exc).__name__}: {exc}")
        summary["traceback"] = traceback.format_exc()[-2000:]
        log.exception("[Pipeline] run failed")

    finally:
        if client is not None:
            await client.aclose()
        summary["duration_s"] = round(
            (datetime.now(timezone.utc) - started).total_seconds(), 1
        )
        if not dry_run:
            store.finish_run(run_id, summary)
        log.info(
            f"── Run {summary['status']}: "
            f"{summary['incidents_created']} new, "
            f"{summary['incidents_updated']} updated, "
            f"{summary['duration_s']}s ──"
        )

    return summary


def run_pipeline_sync(**kwargs) -> dict:
    return asyncio.run(run_pipeline(**kwargs))


# =============================================================================
# CLI
# =============================================================================

def main():

    parser = argparse.ArgumentParser(description="CrisisWatch v5 pipeline")
    parser.add_argument("--once", action="store_true", help="run once and exit")
    parser.add_argument("--dry-run", action="store_true", help="extract but write nothing")
    parser.add_argument("--limit", type=int, default=None, help="max articles this run")
    parser.add_argument("--interval", type=int, default=30, help="scheduler interval (minutes)")
    args = parser.parse_args()

    if args.once or args.dry_run:
        summary = run_pipeline_sync(dry_run=args.dry_run, max_articles=args.limit)
        print("\n── summary ──")
        print(json.dumps(summary, indent=2, default=str))
        return

    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_pipeline_sync,
        "interval",
        minutes=args.interval,
        next_run_time=datetime.now(timezone.utc),
    )
    log.info(f"scheduler started — every {args.interval} minutes")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("stopped")


if __name__ == "__main__":
    main()
