"""Diagnostics: naming the failure precisely enough to act on it."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diagnostics  # noqa: E402
from diagnostics import Recorder, classify  # noqa: E402


class FakeTimeout(Exception):
    pass


FakeTimeout.__name__ = "ReadTimeout"


# ── Classification ──────────────────────────────────────────

@pytest.mark.parametrize("status,expected", [
    (401, diagnostics.AUTH),
    (403, diagnostics.BLOCKED),
    (404, diagnostics.NOT_FOUND),
    (429, diagnostics.RATE_LIMIT),
    (500, diagnostics.UNAVAILABLE),
    (503, diagnostics.UNAVAILABLE),
    (418, diagnostics.HTTP_ERROR),
])
def test_status_codes_map_to_kinds(status, expected):
    assert classify(status=status) == expected


def test_timeout_exception():
    assert classify(exc=FakeTimeout("read timed out")) == diagnostics.TIMEOUT


def test_dns_exception():
    class ConfigurationError(Exception):
        pass

    exc = ConfigurationError("The DNS query name does not exist: _mongodb._tcp.x")
    assert classify(exc=exc) == diagnostics.DNS


def test_connection_exception():
    class ConnectError(Exception):
        pass

    assert classify(exc=ConnectError("connection refused")) == diagnostics.CONNECTION


def test_unknown_exception():
    assert classify(exc=ValueError("something odd")) == diagnostics.UNKNOWN


# ── Recorder ────────────────────────────────────────────────

def test_records_stage_subject_and_kind():
    record = Recorder()
    record.fail_status("fetch", "https://ecdc.europa.eu/x", 403, "Forbidden")

    assert len(record.failures) == 1
    failure = record.failures[0]
    assert failure.stage == "fetch"
    # A random site refusing us is bot protection, not a credential problem.
    assert failure.kind == diagnostics.BLOCKED
    assert failure.status == 403
    assert failure.retryable is False


def test_headline_names_stage_and_kind():
    record = Recorder()
    record.fail("fetch", "https://a.example", diagnostics.TIMEOUT, "slow")
    record.fail("fetch", "https://b.example", diagnostics.TIMEOUT, "slow")
    record.fail("research", "tavily", diagnostics.RATE_LIMIT, "429")

    headline = record.headline()

    assert "fetch/timeout ×2" in headline
    assert "research/rate_limit ×1" in headline


def test_no_failures_says_so():
    assert Recorder().headline() == "no failures"


def test_grouping_by_stage():
    record = Recorder()
    record.fail("collect", "ECDC", diagnostics.EMPTY)
    record.fail("collect", "HealthMap", diagnostics.EMPTY)
    record.fail("extract", "WHO DON", diagnostics.PARSE)

    by_stage = record.by_stage()

    assert by_stage["collect"]["count"] == 2
    assert by_stage["collect"]["kinds"][diagnostics.EMPTY] == 2
    assert by_stage["extract"]["kinds"][diagnostics.PARSE] == 1


def test_advice_is_deduplicated_per_kind():
    record = Recorder()
    record.fail("fetch", "a", diagnostics.TIMEOUT)
    record.fail("fetch", "b", diagnostics.TIMEOUT)

    advice = record.advice()

    assert len(advice) == 1
    assert advice[0]["kind"] == diagnostics.TIMEOUT
    assert advice[0]["advice"]


def test_failures_are_capped_but_counted():
    record = Recorder(max_failures=3)

    for i in range(10):
        record.fail("fetch", f"https://{i}.example", diagnostics.TIMEOUT)

    assert len(record.failures) == 3
    assert record.dropped == 7
    assert record.counts[diagnostics.TIMEOUT] == 10
    assert "+7 more" in record.headline()


def test_long_messages_are_truncated():
    record = Recorder()
    record.fail("fetch", "a", diagnostics.HTTP_ERROR, "x" * 5000)

    assert len(record.failures[0].message) <= diagnostics.MAX_MESSAGE


def test_to_dict_is_serialisable():
    record = Recorder()
    record.fail("fetch", "a", diagnostics.TIMEOUT, "slow")

    payload = record.to_dict()

    assert payload["counts"][diagnostics.TIMEOUT] == 1
    assert payload["failures"][0]["stage"] == "fetch"
    assert payload["headline"]


def test_null_recorder_swallows_everything():
    record = diagnostics.NullRecorder()
    record.fail("fetch", "a", diagnostics.TIMEOUT)

    assert record.failures == []
    assert record.to_dict()["counts"] == {}


# ── The pipeline actually records ───────────────────────────

def test_run_summary_carries_diagnostics(monkeypatch):
    import sources as sources_module
    import pipeline as pipeline_module

    async def fake_collect(client, name, cfg, record=None):
        if record is not None:
            record.fail_status("collect", name, 401, "Unauthorized")
        return []

    monkeypatch.setattr(sources_module, "collect_source", fake_collect)
    monkeypatch.setattr(
        sources_module, "SOURCES",
        {"WHO DON": {"kind": "articles", "parser": "who_don",
                     "url": "https://example.org", "reputation": 0.9,
                     "tier": "HIGH", "limit": 1}},
    )

    summary = asyncio.run(pipeline_module.run_pipeline(dry_run=True))

    assert summary["status"] == "no_sources"
    diag = summary["diagnostics"]
    assert diag["counts"]["auth"] == 1
    assert diag["by_stage"]["collect"]["count"] == 1
    assert "collect/auth" in diag["headline"]
    assert diag["advice"][0]["kind"] == "auth"


# ── Quota and 403 nuance ────────────────────────────────────

def test_429_in_a_message_is_recognised_without_a_status():
    kind = classify(message="ClientError: 429 RESOURCE_EXHAUSTED")
    assert kind == diagnostics.RATE_LIMIT


def test_daily_quota_is_distinguished_from_a_per_minute_limit():
    daily = classify(
        message="429 RESOURCE_EXHAUSTED ... Quota exceeded for metric: "
                "generate_content_free_tier_requests, limit: 20, per day"
    )
    assert daily == diagnostics.QUOTA

    per_minute = classify(
        message="429 RESOURCE_EXHAUSTED ... per minute limit, retryDelay: 22s"
    )
    assert per_minute == diagnostics.RATE_LIMIT


def test_403_from_a_keyed_api_is_a_credential_problem():
    kind = classify(status=403, message="https://generativelanguage.googleapis.com/v1")
    assert kind == diagnostics.AUTH


def test_202_is_a_blocked_challenge_not_an_unknown():
    assert classify(status=202) == diagnostics.BLOCKED


def test_looks_like_daily_quota_helper():
    assert diagnostics.looks_like_daily_quota("limit: 20, per day") is True
    assert diagnostics.looks_like_daily_quota("retryDelay: 22s") is False
