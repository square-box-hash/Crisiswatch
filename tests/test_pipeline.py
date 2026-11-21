"""Incident building, severity derivation, reconciliation.

The first two tests are regressions for the bugs that made live data
impossible: "critical" spread risk silently becoming "moderate", and
"media"/"rumor" claims silently becoming "official".
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import (  # noqa: E402
    ClaimType,
    OutbreakLifecycle,
    SeverityLevel,
    SpreadRisk,
    coerce_claim_type,
    coerce_spread_risk,
)
from pipeline import (  # noqa: E402
    build_incident,
    calibrate_confidence,
    derive_severity,
    reconcile,
)
from sources import Candidate  # noqa: E402


def candidate(reputation=0.95, name="WHO DON", url="https://example.org/a"):
    return Candidate(
        source_name=name,
        url=url,
        reputation=reputation,
        tier="HIGH",
        published_at=datetime(2026, 11, 18, tzinfo=timezone.utc),
    )


def item(**overrides):
    base = {
        "outbreak_detected": True,
        "disease": "Ebola (Bundibugyo)",
        "primary_country": "Democratic Republic of the Congo",
        "estimated_cases": 635,
        "estimated_deaths": 127,
        "confidence": 0.95,
        "claim_type": "official",
        "lifecycle_stage": "active",
        "spread_risk": "high",
        "summary": "635 cases and 127 deaths.",
        "reasoning": "Stated explicitly in the article.",
        "neighboring_mentions": ["Uganda"],
    }
    base.update(overrides)
    return base


# ── Regressions ──────────────────────────────────────────────

def test_critical_spread_risk_survives_enum_coercion():
    assert coerce_spread_risk("critical") is SpreadRisk.CRITICAL
    assert coerce_spread_risk("extreme") is SpreadRisk.CRITICAL
    assert coerce_spread_risk("HIGH") is SpreadRisk.HIGH
    assert coerce_spread_risk("nonsense") is SpreadRisk.MODERATE


def test_critical_spread_risk_produces_critical_severity():
    incident = build_incident(
        item(spread_risk="critical", estimated_cases=5, estimated_deaths=0),
        candidate(),
        {},
    )

    assert incident.spread_risk is SpreadRisk.CRITICAL
    assert incident.severity is SeverityLevel.CRITICAL


def test_media_and_rumor_claims_are_not_upgraded_to_official():
    assert coerce_claim_type("media") is ClaimType.MEDIA_REPORT
    assert coerce_claim_type("news") is ClaimType.MEDIA_REPORT
    assert coerce_claim_type("rumor") is ClaimType.RUMOR
    assert coerce_claim_type("unknown") is ClaimType.UNKNOWN
    assert coerce_claim_type("") is ClaimType.UNKNOWN
    assert coerce_claim_type("official") is ClaimType.OFFICIAL


# ── Severity ─────────────────────────────────────────────────

def test_high_death_rate_is_critical_regardless_of_spread_risk():
    severity = derive_severity(
        SpreadRisk.LOW, OutbreakLifecycle.ACTIVE, cases=2000, deaths=200, population=20_000_000
    )
    assert severity is SeverityLevel.CRITICAL


def test_small_localised_outbreak_is_low():
    severity = derive_severity(
        SpreadRisk.LOW, OutbreakLifecycle.CONTAINED, cases=3, deaths=0, population=50_000_000
    )
    assert severity is SeverityLevel.LOW


# ── Confidence ───────────────────────────────────────────────

def test_confidence_is_tempered_but_news_is_not_buried():
    who = calibrate_confidence(0.95, 0.98)
    news = calibrate_confidence(0.75, 0.72)

    assert who <= 0.99
    assert 0.60 <= news <= 0.75, news


def test_confidence_never_exceeds_cap():
    assert calibrate_confidence(1.0, 1.0) <= 0.99


# ── Incident building ────────────────────────────────────────

def test_incident_carries_case_counts_and_provenance():
    incident = build_incident(item(), candidate(), {"DRC": "monsoon_flood"})

    assert incident.estimated_cases == 635
    assert incident.estimated_deaths == 127
    assert incident.source_urls == ["https://example.org/a"]
    assert incident.source_names == ["WHO DON"]
    assert incident.incident_key == "ebola-bundibugyo:democratic-republic-of-the-congo"
    assert incident.trigger is None  # keyed on exact country name
    assert incident.published_at.year == 2026
    assert incident.verification_status.value == "unverified"


def test_incident_is_bson_safe():
    doc = build_incident(item(), candidate(), {}).to_doc()

    assert doc["severity"] == "high"
    assert doc["spread_risk"] == "high"
    assert doc["claim_type"] == "official"
    assert isinstance(doc["severity"], str)


def test_missing_country_returns_none():
    assert build_incident(item(primary_country=""), candidate(), {}) is None


# ── Reconciliation ───────────────────────────────────────────

def test_same_outbreak_from_two_sources_merges():
    a = build_incident(item(estimated_cases=635), candidate(url="https://a.org/1"), {})
    b = build_incident(
        item(estimated_cases=700, confidence=0.8),
        candidate(name="CDC Travel Notices", url="https://b.org/2"),
        {},
    )

    merged = reconcile([a, b])

    assert len(merged) == 1
    assert merged[0].estimated_cases == 700
    assert merged[0].source_count == 2
    assert set(merged[0].source_urls) == {"https://a.org/1", "https://b.org/2"}
    assert set(merged[0].source_names) == {"WHO DON", "CDC Travel Notices"}


def test_different_outbreaks_are_not_merged():
    a = build_incident(item(), candidate(), {})
    b = build_incident(item(disease="Cholera", primary_country="Zambia"), candidate(), {})

    assert len(reconcile([a, b])) == 2
