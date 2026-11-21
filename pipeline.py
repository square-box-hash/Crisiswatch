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
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

import db
import sources
import store
from extract import extract_from_candidate, init_client
from fetch import fetch_page, gather_limited, make_client
import assess
import research
import synthesis
from models import (
    CanonicalIncident,
    Evidence,
    OutbreakStatus,
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
    cases = cases or 0
    deaths = deaths or 0
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


def refresh_from_evidence(incident) -> None:
    """Recompute figures, provenance and status from the evidence ledger."""

    entries = [
        asdict(e) if isinstance(e, Evidence) else e for e in incident.evidence
    ]
    summary = assess.summarise_evidence(entries)

    incident.estimated_cases = summary["cases"]
    incident.estimated_deaths = summary["deaths"]
    incident.cases_source = summary["cases_source"]
    incident.cases_as_of = summary["cases_as_of"]
    incident.cases_unknown = summary["cases_unknown"]
    incident.status = OutbreakStatus(summary["status"])
    incident.source_count = len(incident.evidence)

    incident.severity = derive_severity(
        incident.spread_risk,
        incident.lifecycle_stage,
        incident.estimated_cases,
        incident.estimated_deaths,
        incident.population,
    )


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

    # None means "no source stated a figure". It is not zero.
    raw_cases = item.get("estimated_cases")
    raw_deaths = item.get("estimated_deaths")

    cases = int(raw_cases) if raw_cases is not None else None
    deaths = int(raw_deaths) if raw_deaths is not None else None

    lifecycle = coerce_lifecycle(item.get("lifecycle_stage"))
    claim_type = coerce_claim_type(item.get("claim_type"))
    spread_risk = coerce_spread_risk(item.get("spread_risk"))

    population = POP.get(country, 10_000_000)

    confidence = calibrate_confidence(
        float(item.get("confidence", 0.5)), candidate.reputation
    )

    severity = derive_severity(spread_risk, lifecycle, cases, deaths, population)

    key = store.incident_key(disease, country)

    evidence = [
        Evidence(
            source_name=candidate.source_name,
            url=candidate.url,
            tier=candidate.tier,
            published_at=candidate.published_at,
            cases=cases,
            deaths=deaths,
            confidence=confidence,
            excerpt=(getattr(candidate, "text", "") or "")[:1200],
        )
    ]

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
        cases_unknown=cases is None,
        evidence=evidence,
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

            # Every source's claim, not just the winner's.
            merged_evidence = []
            for g in group:
                merged_evidence.extend(g.evidence)

            primary.evidence = merged_evidence
            primary.source_urls = list({u for g in group for u in g.source_urls if u})
            primary.source_names = list({n for g in group for n in g.source_names if n})
            primary.source_count = len(primary.source_urls)

            primary.neighboring_mentions = list(
                {c for g in group for c in g.neighboring_mentions}
            )

            # Figures come from assess: official sources first, never invented.
            refresh_from_evidence(primary)

        merged.append(primary)

    return merged


# =============================================================================
# Pipeline
# =============================================================================

def select_candidates(by_source: dict, limit: int) -> list:
    """
    Take turns across sources so a small cap still samples every source.

    Sorting all candidates by published_at and slicing looked reasonable but
    starved every source without dates (WHO DON, ReliefWeb, ProMED) — with
    limit=8 only CDC notices survived, and CDC notices carry no case counts.
    """

    queues = []

    for name, items in by_source.items():
        items = [c for c in items if c]
        items.sort(
            key=lambda c: c.published_at
            or datetime(1970, 1, 1, tzinfo=timezone.utc),
            reverse=True,
        )
        if items:
            queues.append(items)

    selected, seen = [], set()

    while len(selected) < limit and any(queues):
        progressed = False
        for queue in queues:
            if not queue:
                continue
            candidate = queue.pop(0)
            progressed = True
            if candidate.key() in seen:
                continue
            seen.add(candidate.key())
            selected.append(candidate)
            if len(selected) >= limit:
                break
        if not progressed:
            break

    return selected


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

    if not dry_run:
        # Close out runs orphaned by an instance restart, then refuse to start
        # a second concurrent run (two runs double the Gemini spend and race on
        # the same documents).
        store.mark_stale_runs()

        active = store.active_run()
        if active:
            log.warning(
                f"[Runs] already running since {active.get('started_at')} — skipping"
            )
            return {
                "status": "already_running",
                "run_id": str(active.get("_id")),
                "started_at": str(active.get("started_at")),
                "errors": [],
            }

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

        by_source = {}

        for (name, _cfg), result in zip(sources.SOURCES.items(), collected):
            if isinstance(result, Exception):
                summary["errors"].append(f"{name}: {type(result).__name__}: {result}")
                summary["sources"][name] = 0
                continue
            summary["sources"][name] = len(result)
            by_source[name] = list(result)

        total = sum(len(v) for v in by_source.values())
        summary["candidates"] = total
        log.info(
            f"[Collect] {total} candidate(s) from "
            f"{sum(1 for v in by_source.values() if v)}/{len(sources.SOURCES)} source(s)"
        )

        if total == 0:
            summary["status"] = "no_sources"
            summary["errors"].append(
                "No candidates from any source — every feed returned nothing. "
                "Check the [Source] log lines above."
            )
            return summary

        candidates = select_candidates(by_source, limit)
        log.info(
            "[Select] "
            + ", ".join(
                f"{name}:{sum(1 for c in candidates if c.source_name == name)}"
                for name in by_source
                if any(c.source_name == name for c in candidates)
            )
        )

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

        # ── 5a. Research the records that are still thin ──
        if research.enabled():
            thin = [i for i in incidents if research.needs_research(i)]
            thin.sort(key=lambda i: (i.estimated_cases is not None, len(i.evidence)))
            thin = thin[: research.MAX_RESEARCH_PER_RUN]

            if thin:
                log.info(
                    f"[Research] {len(thin)} record(s) without a stated figure"
                )

                found = await gather_limited(
                    [research.research_incident(client, i) for i in thin],
                    limit=2,
                )

                attempted_at = datetime.now(timezone.utc)

                for incident, extra in zip(thin, found):
                    # Record the attempt either way, so a record that stays
                    # unknown is not re-searched on the next run.
                    incident.metadata = {
                        **(incident.metadata or {}),
                        "research": {
                            "last_attempt": attempted_at,
                            "found": len(extra or []),
                        },
                    }

                    if not extra:
                        continue
                    incident.evidence = list(incident.evidence) + extra
                    incident.source_urls = sorted(
                        {e.url for e in incident.evidence if e.url}
                    )
                    incident.source_names = sorted(
                        {e.source_name for e in incident.evidence if e.source_name}
                    )
                    refresh_from_evidence(incident)
                    log.info(
                        f"[Research] {incident.disease}/{incident.country} now "
                        f"{incident.estimated_cases if incident.estimated_cases is not None else 'unknown'} "
                        f"cases, {incident.status.value}"
                    )

        # ── 5b. Cross-source synthesis ──
        # Only worth a call when more than one source described the outbreak.
        multi = [i for i in incidents if len(i.evidence) >= 2]

        if multi:
            synth_results = await gather_limited(
                [
                    synthesis.synthesise(
                        i.disease,
                        i.country,
                        [asdict(e) for e in i.evidence],
                    )
                    for i in multi
                ],
                limit=GEMINI_CONCURRENCY,
            )

            for incident, result in zip(multi, synth_results):
                if not result:
                    continue

                if result["summary"]:
                    incident.ai_summary = result["summary"]
                if result["reasoning"]:
                    incident.reasoning = result["reasoning"]

                incident.metadata = {
                    **(incident.metadata or {}),
                    "synthesis": {
                        "agreement": result["agreement"],
                        "contradictions": result["contradictions"],
                        "same_event": result["same_event"],
                        "confidence": result["confidence"],
                    },
                }

            log.info(f"[Synthesis] reconciled {len(multi)} multi-source incident(s)")

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
