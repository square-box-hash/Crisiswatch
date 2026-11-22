"""
Research: go and look for the missing numbers.

Extraction only knows what the feeds handed it. When an outbreak arrives with
no stated case count — or from a single weak source — this asks Tavily for
better sources, fetches them, and runs the same extraction over the results.

Every result becomes an ordinary Evidence entry, so it lands in the ledger and
is subject to the same rules: a figure must come from a URL we actually read.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import diagnostics
from assess import derive_status
from extract import extract_from_candidate
from fetch import fetch_page
from models import Evidence, OutbreakStatus
from sources import Candidate

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

MAX_RESEARCH_PER_RUN = int(os.getenv("MAX_RESEARCH_PER_RUN", "3"))
# Don't re-search a record that was already looked at recently: a record that
# stays unknown would otherwise burn credits on every run (48 runs/day at a
# 30-minute schedule is 4,300 searches a month on a 1,000-credit plan).
COOLDOWN_HOURS = int(os.getenv("RESEARCH_COOLDOWN_HOURS", "24"))
PAGES_PER_INCIDENT = int(os.getenv("RESEARCH_PAGES", "3"))
SEARCH_DEPTH = os.getenv("TAVILY_SEARCH_DEPTH", "basic")

# Which domains count as official. Anything else is treated as media.
OFFICIAL_DOMAINS = {
    "who.int": ("HIGH", 0.97),
    "afro.who.int": ("HIGH", 0.96),
    "cdc.gov": ("HIGH", 0.95),
    "ecdc.europa.eu": ("HIGH", 0.94),
    "paho.org": ("HIGH", 0.93),
    "reliefweb.int": ("HIGH", 0.92),
    "promedmail.org": ("HIGH", 0.90),
    "un.org": ("HIGH", 0.88),
    "healthmap.org": ("MEDIUM", 0.78),
    "reuters.com": ("MEDIUM", 0.70),
    "apnews.com": ("MEDIUM", 0.70),
}

# Statuses worth spending a search credit on.
RESEARCH_STATUSES = {OutbreakStatus.SIGNAL, OutbreakStatus.SUSPECTED}

COUNTRY_ALIASES = {
    "drc": "democratic republic of the congo",
    "dr congo": "democratic republic of the congo",
    "congo": "democratic republic of the congo",
    "usa": "united states",
    "us": "united states",
    "uk": "united kingdom",
}


def enabled() -> bool:
    return bool(TAVILY_API_KEY)


def _norm(value: str) -> str:
    value = (value or "").strip().lower()
    return COUNTRY_ALIASES.get(value, value)


def tier_for_url(url: str) -> tuple:
    """Official domains keep their standing; everything else is media."""

    host = (urlparse(url).hostname or "").lower()

    for domain, (tier, reputation) in OFFICIAL_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return tier, reputation

    return "LOW", 0.45


def recently_researched(incident, hours: int = COOLDOWN_HOURS) -> bool:
    """Has this record already had a search spent on it lately?"""

    metadata = getattr(incident, "metadata", None) or {}
    last = (metadata.get("research") or {}).get("last_attempt")

    if not last:
        return False

    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return False

    if not isinstance(last, datetime):
        return False

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    return (datetime.now(timezone.utc) - last) < timedelta(hours=hours)


def needs_research(incident) -> bool:
    """No stated figure, or only weakly corroborated — worth a search."""

    if recently_researched(incident):
        return False

    if incident.estimated_cases is None:
        return True
    return incident.status in RESEARCH_STATUSES


def research_query(disease: str, country: str) -> str:
    year = datetime.now(timezone.utc).year
    return f"{disease} {country} outbreak confirmed cases {year}"


def build_candidate(result: dict, text: str) -> Candidate:
    url = result.get("url") or ""
    tier, reputation = tier_for_url(url)
    host = (urlparse(url).hostname or "unknown").removeprefix("www.")

    published = None
    raw = result.get("published_date")
    if isinstance(raw, str):
        try:
            published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            published = None

    return Candidate(
        source_name=host,
        url=url,
        title=(result.get("title") or "").strip(),
        published_at=published,
        reputation=reputation,
        tier=tier,
        inline_text=text,
    )


def same_outbreak(item: dict, disease: str, country: str) -> bool:
    """
    Guard against a search result about a different outbreak of the same
    disease, or a different disease in the same country.
    """

    item_country = _norm(str(item.get("primary_country") or ""))
    neighbours = [_norm(str(n)) for n in (item.get("neighboring_mentions") or [])]
    target_country = _norm(country)

    if item_country != target_country and target_country not in neighbours:
        return False

    item_disease = re.sub(r"[^a-z0-9 ]", " ", str(item.get("disease") or "").lower())
    target_disease = re.sub(r"[^a-z0-9 ]", " ", (disease or "").lower())

    item_tokens = {t for t in item_disease.split() if len(t) > 3}
    target_tokens = {t for t in target_disease.split() if len(t) > 3}

    if not item_tokens or not target_tokens:
        return False

    return bool(item_tokens & target_tokens)


async def search(client, query: str, max_results: int = 5, record=None) -> list:
    """Tavily search. Returns [] on any failure — research is optional."""

    if not enabled():
        if record is not None:
            record.fail("research", "tavily", diagnostics.NO_KEY,
                        "TAVILY_API_KEY is not set — research is disabled")
        return []

    payload = {
        "query": query,
        "search_depth": SEARCH_DEPTH,
        "max_results": max_results,
        "topic": "general",
        "chunks_per_source": 3,
        # Sent both ways: older deployments read api_key from the body, current
        # ones accept the bearer header.
        "api_key": TAVILY_API_KEY,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TAVILY_API_KEY}",
    }

    try:
        resp = await client.post(TAVILY_URL, json=payload, headers=headers, timeout=30)
    except Exception as exc:
        log.warning(f"[Research] search failed: {type(exc).__name__}: {exc}")
        if record is not None:
            record.fail_exc("research", "tavily", exc, url=TAVILY_URL)
        return []

    if resp.status_code != 200:
        log.warning(
            f"[Research] search HTTP {resp.status_code}: {resp.text[:200]}"
        )
        if record is not None:
            record.fail_status(
                "research", "tavily", resp.status_code, resp.text[:200], url=TAVILY_URL
            )
        return []

    try:
        data = resp.json()
    except Exception as exc:
        log.warning("[Research] search returned non-JSON")
        if record is not None:
            record.fail_exc("research", "tavily", exc, status=200, url=TAVILY_URL)
        return []

    results = data.get("results") or []

    if not results and record is not None:
        record.fail("research", "tavily", diagnostics.EMPTY,
                    f"search for {query!r} returned no results", url=TAVILY_URL)

    log.info(f"[Research] '{query}' → {len(results)} result(s)")
    return results


async def research_incident(client, incident, record=None) -> list:
    """
    Search, fetch, extract, and return the extra Evidence for one outbreak.
    Never raises: a failed lookup just leaves the record as it was.
    """

    if not enabled():
        return []

    query = research_query(incident.disease, incident.country)
    results = await search(client, query, max_results=PAGES_PER_INCIDENT + 2, record=record)

    if not results:
        return []

    known = {str(e.url).split("?")[0].rstrip("/").lower() for e in incident.evidence}
    extra: list = []

    for result in results:
        if len(extra) >= PAGES_PER_INCIDENT:
            break

        url = result.get("url") or ""
        if not url or url.split("?")[0].rstrip("/").lower() in known:
            continue

        # Prefer the real page; fall back to Tavily's snippet.
        text = await fetch_page(client, url, max_chars=10_000, record=record)
        if not text:
            text = (result.get("content") or "").strip()

        if not text or len(text) < 200:
            continue

        candidate = build_candidate(result, text)
        candidate.text = text

        items, error = await extract_from_candidate(candidate, attempts=2, record=record)

        if error:
            log.info(f"[Research] {url} → {error}")
            continue

        for item in items:
            if not same_outbreak(item, incident.disease, incident.country):
                continue

            raw_cases = item.get("estimated_cases")
            raw_deaths = item.get("estimated_deaths")

            extra.append(
                Evidence(
                    source_name=candidate.source_name,
                    url=url,
                    tier=candidate.tier,
                    published_at=candidate.published_at,
                    cases=int(raw_cases) if raw_cases is not None else None,
                    deaths=int(raw_deaths) if raw_deaths is not None else None,
                    confidence=float(item.get("confidence") or 0.5),
                    excerpt=text[:1200],
                )
            )

    if not extra and record is not None:
        record.fail(
            "research", f"{incident.disease}/{incident.country}", diagnostics.NO_MATCH,
            f"{len(results)} result(s) returned, none matched this outbreak",
        )

    with_figures = sum(1 for e in extra if e.cases is not None)
    log.info(
        f"[Research] {incident.disease}/{incident.country} → "
        f"{len(extra)} source(s), {with_figures} with a stated figure"
    )

    return extra
