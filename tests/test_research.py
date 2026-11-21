"""Tavily research: query shape, domain trust, matching, and the flow."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import research  # noqa: E402
from models import Evidence, OutbreakStatus  # noqa: E402


def incident(cases=None, status=OutbreakStatus.SIGNAL, evidence=None,
             disease="Typhoid Fever", country="Senegal"):
    return SimpleNamespace(
        disease=disease,
        country=country,
        estimated_cases=cases,
        estimated_deaths=None,
        status=status,
        evidence=evidence or [],
        source_urls=[],
        source_names=[],
    )


# ── When to spend a credit ───────────────────────────────────

def test_unknown_count_needs_research():
    assert research.needs_research(incident(cases=None)) is True


def test_single_source_needs_research_even_with_a_number():
    assert research.needs_research(
        incident(cases=412, status=OutbreakStatus.SUSPECTED)
    ) is True


def test_confirmed_record_does_not_need_research():
    assert research.needs_research(
        incident(cases=412, status=OutbreakStatus.CONFIRMED)
    ) is False


# ── Query and domains ────────────────────────────────────────

def test_query_names_the_disease_country_and_year():
    query = research.research_query("Typhoid Fever", "Senegal")

    assert "Typhoid Fever" in query
    assert "Senegal" in query
    assert str(datetime.now(timezone.utc).year) in query
    assert "cases" in query


def test_official_domains_are_trusted():
    assert research.tier_for_url("https://www.who.int/emergencies/x")[0] == "HIGH"
    assert research.tier_for_url("https://reliefweb.int/report/x")[0] == "HIGH"
    assert research.tier_for_url("https://afro.who.int/x")[0] == "HIGH"


def test_unknown_domains_are_media():
    tier, reputation = research.tier_for_url("https://someblog.example/x")
    assert tier == "LOW"
    assert reputation < 0.6


def test_a_lookalike_domain_is_not_treated_as_official():
    # evil-who.int must not inherit who.int's standing
    assert research.tier_for_url("https://evil-who.int/x")[0] == "LOW"


# ── Matching the right outbreak ──────────────────────────────

def test_matching_item_is_accepted():
    item = {"disease": "Typhoid Fever", "primary_country": "Senegal"}
    assert research.same_outbreak(item, "Typhoid Fever", "Senegal") is True


def test_wrong_country_is_rejected():
    item = {"disease": "Typhoid Fever", "primary_country": "Zimbabwe"}
    assert research.same_outbreak(item, "Typhoid Fever", "Senegal") is False


def test_wrong_disease_is_rejected():
    item = {"disease": "Measles", "primary_country": "Senegal"}
    assert research.same_outbreak(item, "Typhoid Fever", "Senegal") is False


def test_country_alias_is_accepted():
    item = {"disease": "Ebola", "primary_country": "DRC"}
    assert research.same_outbreak(
        item, "Ebola (Bundibugyo)", "Democratic Republic of the Congo"
    ) is True


def test_neighbour_mention_counts_as_a_match():
    item = {
        "disease": "Cholera",
        "primary_country": "Zambia",
        "neighboring_mentions": ["Malawi"],
    }
    assert research.same_outbreak(item, "Cholera", "Malawi") is True


# ── Candidate building ──────────────────────────────────────

def test_candidate_takes_its_name_and_tier_from_the_url():
    candidate = research.build_candidate(
        {
            "url": "https://www.who.int/emergencies/don/1",
            "title": "Typhoid – Senegal",
            "published_date": "2026-11-18T00:00:00Z",
        },
        "text " * 100,
    )

    assert candidate.source_name == "who.int"
    assert candidate.tier == "HIGH"
    assert candidate.published_at.year == 2026


# ── The whole flow ──────────────────────────────────────────

def test_research_returns_evidence_and_skips_known_urls(monkeypatch):
    monkeypatch.setattr(research, "TAVILY_API_KEY", "test-key")

    async def fake_search(client, query, max_results=5):
        return [
            {"url": "https://www.who.int/don/typhoid-senegal", "title": "Typhoid"},
            {"url": "https://known.example/already-have-it", "title": "Old"},
            {"url": "https://news.example/typhoid", "title": "News"},
        ]

    async def fake_fetch(client, url, max_chars=10000):
        return "Typhoid fever outbreak in Senegal with 412 confirmed cases. " * 10

    async def fake_extract(candidate, attempts=3):
        if "who.int" in candidate.url:
            return (
                [{"disease": "Typhoid Fever", "primary_country": "Senegal",
                  "estimated_cases": 412, "estimated_deaths": 3, "confidence": 0.9}],
                "",
            )
        return ([{"disease": "Typhoid Fever", "primary_country": "Zimbabwe",
                  "estimated_cases": 99, "confidence": 0.6}], "")

    monkeypatch.setattr(research, "search", fake_search)
    monkeypatch.setattr(research, "fetch_page", fake_fetch)
    monkeypatch.setattr(research, "extract_from_candidate", fake_extract)

    known = Evidence(
        source_name="cdc.gov",
        url="https://known.example/already-have-it",
        tier="HIGH",
        cases=None,
    )

    extra = asyncio.run(research.research_incident(None, incident(evidence=[known])))

    # The URL we already had is skipped; the wrong-country hit is dropped.
    assert len(extra) == 1
    assert extra[0].source_name == "who.int"
    assert extra[0].cases == 412
    assert extra[0].tier == "HIGH"


def test_research_is_inert_without_a_key(monkeypatch):
    monkeypatch.setattr(research, "TAVILY_API_KEY", "")
    assert asyncio.run(research.research_incident(None, incident())) == []


# ── Cooldown: don't re-search a record every run ────────────

def test_recent_attempt_blocks_a_second_search():
    from datetime import datetime, timedelta, timezone

    recent = incident(cases=None)
    recent.metadata = {
        "research": {"last_attempt": datetime.now(timezone.utc) - timedelta(hours=2)}
    }

    assert research.recently_researched(recent) is True
    assert research.needs_research(recent) is False


def test_old_attempt_does_not_block():
    from datetime import datetime, timedelta, timezone

    stale = incident(cases=None)
    stale.metadata = {
        "research": {"last_attempt": datetime.now(timezone.utc) - timedelta(hours=30)}
    }

    assert research.recently_researched(stale) is False
    assert research.needs_research(stale) is True


def test_never_researched_is_not_blocked():
    fresh = incident(cases=None)
    fresh.metadata = {}

    assert research.recently_researched(fresh) is False


def test_naive_timestamps_from_mongo_do_not_crash():
    from datetime import datetime

    naive = incident(cases=None)
    naive.metadata = {"research": {"last_attempt": datetime.utcnow()}}

    assert research.recently_researched(naive) is True
