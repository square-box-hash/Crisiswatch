"""Assessment rules: honest counts, corroboration, status buckets."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assess import (  # noqa: E402
    best_counts,
    derive_status,
    is_active,
    merge_evidence,
    summarise_evidence,
)
from models import Evidence, OutbreakStatus  # noqa: E402


def ev(source, cases, tier="HIGH", url=None, deaths=None, published=None):
    return Evidence(
        source_name=source,
        url=url or f"https://example.org/{source.lower().replace(' ', '-')}",
        tier=tier,
        cases=cases,
        deaths=deaths,
        published_at=published,
        confidence=0.8,
    )


# ── Counts ───────────────────────────────────────────────────

def test_no_stated_figure_is_unknown_not_zero():
    cases, deaths, url, as_of = best_counts([ev("CDC Travel Notices", None)])

    assert cases is None
    assert deaths is None
    assert url is None
    assert as_of is None


def test_official_count_wins_over_media():
    cases, _, url, _ = best_counts(
        [
            ev("Outbreak News Today", 9000, tier="MEDIUM", url="https://news.example/a"),
            ev("WHO DON", 412, url="https://who.int/don/1"),
        ]
    )

    assert cases == 412
    assert url == "https://who.int/don/1"


def test_highest_official_count_is_used():
    cases, _, url, _ = best_counts(
        [
            ev("WHO DON", 412, url="https://who.int/a"),
            ev("ReliefWeb", 520, url="https://reliefweb.int/b"),
        ]
    )

    assert cases == 520
    assert url == "https://reliefweb.int/b"


def test_unknown_never_beats_a_stated_figure():
    cases, _, _, _ = best_counts(
        [ev("WHO DON", 635), ev("CDC Travel Notices", None)]
    )
    assert cases == 635


def test_deaths_fall_back_to_another_source_when_missing():
    cases, deaths, _, _ = best_counts(
        [
            ev("WHO DON", 635, deaths=None),
            ev("ReliefWeb", 600, deaths=127),
        ]
    )
    assert cases == 635
    assert deaths == 127


# ── Status ───────────────────────────────────────────────────

def test_two_sources_with_official_counts_is_confirmed():
    assert derive_status(
        [ev("WHO DON", 635), ev("ReliefWeb", 640)]
    ) is OutbreakStatus.CONFIRMED


def test_one_official_source_with_counts_is_probable():
    assert derive_status([ev("WHO DON", 635)]) is OutbreakStatus.PROBABLE


def test_two_sources_without_counts_is_suspected():
    assert derive_status(
        [ev("CDC Travel Notices", None), ev("Outbreak News Today", None, tier="MEDIUM")]
    ) is OutbreakStatus.SUSPECTED


def test_single_source_without_counts_is_only_a_signal():
    assert derive_status([ev("CDC Travel Notices", None)]) is OutbreakStatus.SIGNAL


def test_no_evidence_is_signal():
    assert derive_status([]) is OutbreakStatus.SIGNAL


def test_media_only_with_numbers_is_not_confirmed():
    assert derive_status(
        [ev("Outbreak News Today", 500, tier="MEDIUM"), ev("ProMED", 480, tier="MEDIUM")]
    ) is OutbreakStatus.SUSPECTED


# ── Merging evidence ─────────────────────────────────────────

def test_same_url_is_not_double_counted():
    merged = merge_evidence(
        [{"url": "https://a.org/x", "cases": 10, "source_name": "A"}],
        [{"url": "https://a.org/x/", "cases": 12, "source_name": "A"}],
    )
    assert len(merged) == 1


def test_merging_keeps_a_known_figure_when_the_other_is_unknown():
    merged = merge_evidence(
        [{"url": "https://a.org/x", "cases": 635}],
        [{"url": "https://a.org/x", "cases": None}],
    )
    assert merged[0]["cases"] == 635


def test_distinct_urls_accumulate():
    merged = merge_evidence(
        [{"url": "https://a.org/x", "cases": 10}],
        [{"url": "https://b.org/y", "cases": None}],
    )
    assert len(merged) == 2


# ── Summary ──────────────────────────────────────────────────

def test_summarise_marks_unknown_cases():
    summary = summarise_evidence(
        [{"url": "https://a.org/x", "source_name": "CDC", "tier": "HIGH", "cases": None}]
    )

    assert summary["cases"] is None
    assert summary["cases_unknown"] is True
    assert summary["status"] == "signal"


def test_summarise_counts_distinct_sources():
    summary = summarise_evidence(
        [
            {"url": "https://a.org/x", "source_name": "WHO DON", "tier": "HIGH", "cases": 635},
            {"url": "https://b.org/y", "source_name": "ReliefWeb", "tier": "HIGH", "cases": 640},
        ]
    )

    assert summary["cases"] == 640
    assert summary["sources_agreeing"] == 2
    assert summary["status"] == "confirmed"


# ── Active window ────────────────────────────────────────────

def test_recent_is_active_and_old_is_not():
    now = datetime.now(timezone.utc)

    assert is_active(now - timedelta(days=2)) is True
    assert is_active(now - timedelta(days=30)) is False
    assert is_active(None) is False
