"""
Assessment: turn a pile of source claims into one honest record.

Two rules drive everything here:

  1. "No figure stated" is not zero. `None` means unknown, and unknown must
     never overwrite a number another source did state.
  2. One source is a claim; several independent sources are evidence. The
     status bucket records which one you are looking at.
"""

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from models import Evidence, OutbreakStatus

# Sources whose tier is HIGH are official / professionally moderated.
OFFICIAL_TIERS = {"HIGH"}

# An outbreak nobody has mentioned for this long is no longer "active".
ACTIVE_WINDOW_DAYS = 14


def is_official(evidence: Evidence) -> bool:
    return (evidence.tier or "").upper() in OFFICIAL_TIERS


def distinct_sources(evidence: Iterable[Evidence]) -> set:
    return {e.source_name for e in evidence if e.source_name}


def stated_counts(evidence: Iterable[Evidence]) -> list:
    return [e for e in evidence if e.cases is not None]


def best_counts(evidence: Iterable[Evidence]) -> tuple:
    """
    The headline figures, and where they came from.

    Prefers an official source that stated a number; among those, the highest
    count (outbreak figures are cumulative, so the max is the most recent
    picture, not the oldest). Returns (cases, deaths, source_url, as_of).
    """

    evidence = list(evidence)
    stated = stated_counts(evidence)

    if not stated:
        return None, None, None, None

    official = [e for e in stated if is_official(e)]
    pool = official or stated

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    best = max(pool, key=lambda e: (int(e.cases or 0), e.published_at or epoch))

    # Deaths: use the same source when it stated them, else the highest stated.
    if best.deaths is not None:
        deaths = best.deaths
    else:
        stated_deaths = [e.deaths for e in pool if e.deaths is not None]
        deaths = max(stated_deaths) if stated_deaths else None

    return best.cases, deaths, best.url, best.published_at


def derive_status(evidence: Iterable[Evidence]) -> OutbreakStatus:
    """How corroborated is this, ignoring how severe it is."""

    evidence = list(evidence)

    if not evidence:
        return OutbreakStatus.SIGNAL

    sources = distinct_sources(evidence)
    official = [e for e in evidence if is_official(e)]
    official_with_counts = [e for e in official if e.cases is not None]

    if official_with_counts and len(sources) >= 2:
        return OutbreakStatus.CONFIRMED

    if official_with_counts:
        return OutbreakStatus.PROBABLE

    if len(sources) >= 2:
        return OutbreakStatus.SUSPECTED

    return OutbreakStatus.SIGNAL


def is_active(last_seen: Optional[datetime], days: int = ACTIVE_WINDOW_DAYS) -> bool:
    if not isinstance(last_seen, datetime):
        return False

    # pymongo hands back naive datetimes (UTC by convention) unless the client
    # is created with tz_aware=True, so normalise before comparing.
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)

    return (datetime.now(timezone.utc) - last_seen) <= timedelta(days=days)


def merge_evidence(existing: Iterable[dict], incoming: Iterable[dict]) -> list:
    """
    Union of evidence by URL. A newer claim from the same URL replaces the old
    one; a claim with no figures never erases figures another entry stated.
    """

    merged: dict[str, dict] = {}

    def key(entry: dict) -> str:
        return (entry.get("url") or "").split("?")[0].rstrip("/").lower()

    for entry in list(existing) + list(incoming):
        k = key(entry)
        if not k:
            continue
        current = merged.get(k)
        if current is None:
            merged[k] = dict(entry)
            continue

        # Keep the figures that are known.
        if current.get("cases") is None and entry.get("cases") is not None:
            current["cases"] = entry["cases"]
        if current.get("deaths") is None and entry.get("deaths") is not None:
            current["deaths"] = entry["deaths"]

        current["confidence"] = max(
            float(current.get("confidence") or 0),
            float(entry.get("confidence") or 0),
        )
        current["source_name"] = current.get("source_name") or entry.get("source_name")

    return list(merged.values())


def evidence_to_objects(entries: Iterable[dict]) -> list:
    """Stored dicts → Evidence objects, for the pure helpers above."""

    out = []

    for entry in entries or []:
        if isinstance(entry, Evidence):
            out.append(entry)
            continue
        out.append(
            Evidence(
                source_name=entry.get("source_name") or "unknown",
                url=entry.get("url") or "",
                tier=entry.get("tier") or "MEDIUM",
                published_at=entry.get("published_at"),
                cases=entry.get("cases"),
                deaths=entry.get("deaths"),
                confidence=float(entry.get("confidence") or 0.5),
                excerpt=entry.get("excerpt") or "",
            )
        )

    return out


def summarise_evidence(entries: Iterable[dict]) -> dict:
    """Everything the store and the API need to record about a record's support."""

    objects = evidence_to_objects(entries)
    cases, deaths, source_url, as_of = best_counts(objects)

    return {
        "status": derive_status(objects).value,
        "cases": cases,
        "deaths": deaths,
        "cases_source": source_url,
        "cases_as_of": as_of,
        "cases_unknown": cases is None,
        "sources_agreeing": len(distinct_sources(objects)),
    }
