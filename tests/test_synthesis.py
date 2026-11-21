"""Cross-source synthesis: prompt building and response hygiene."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from synthesis import (  # noqa: E402
    build_evidence_block,
    build_prompt,
    normalise_result,
    parse_object,
)

EVIDENCE = [
    {
        "source_name": "WHO DON",
        "url": "https://who.int/don/1",
        "tier": "HIGH",
        "published_at": datetime(2026, 11, 18, tzinfo=timezone.utc),
        "cases": 635,
        "deaths": 127,
        "excerpt": "635 confirmed cases including 127 deaths.",
    },
    {
        "source_name": "CDC Travel Notices",
        "url": "https://cdc.gov/n/2",
        "tier": "HIGH",
        "published_at": None,
        "cases": None,
        "deaths": None,
        "excerpt": "Ebola transmission is ongoing in the region.",
    },
]


def test_block_shows_urls_and_says_not_stated():
    block = build_evidence_block(EVIDENCE)

    assert "https://who.int/don/1" in block
    assert "cases: 635" in block
    assert "cases: not stated" in block
    assert block.index("WHO DON") < block.index("CDC")


def test_prompt_carries_disease_country_and_all_sources():
    prompt = build_prompt("Ebola (Bundibugyo)", "Kenya", EVIDENCE)

    assert "Ebola (Bundibugyo)" in prompt
    assert "Kenya" in prompt
    assert prompt.count("URL:") == 2
    assert "never 0" in prompt.lower() or "never 0" in prompt


def test_parse_object_handles_fences_and_prose():
    assert parse_object('```json\n{"cases": 5}\n```') == {"cases": 5}
    assert parse_object('Here you go: {"cases": 5} done') == {"cases": 5}
    assert parse_object("not json at all") is None
    assert parse_object("") is None


def test_null_cases_survive_normalisation():
    result = normalise_result({"cases": None, "deaths": None}, EVIDENCE)

    assert result["cases"] is None
    assert result["deaths"] is None


def test_figure_with_an_unknown_url_is_not_attributed():
    result = normalise_result(
        {"cases": 999, "cases_source_url": "https://made-up.example/x"}, EVIDENCE
    )

    assert result["cases"] == 999
    assert result["cases_source_url"] == ""


def test_figure_with_a_known_url_is_kept():
    result = normalise_result(
        {"cases": 635, "cases_source_url": "https://who.int/don/1"}, EVIDENCE
    )

    assert result["cases_source_url"] == "https://who.int/don/1"


def test_bad_agreement_value_falls_back():
    assert normalise_result({"agreement": "banana"}, EVIDENCE)["agreement"] == "partial"
    assert normalise_result({"agreement": "conflict"}, EVIDENCE)["agreement"] == "conflict"


def test_confidence_is_clamped():
    assert normalise_result({"confidence": 7}, EVIDENCE)["confidence"] == 1.0
    assert normalise_result({"confidence": -1}, EVIDENCE)["confidence"] == 0.0


def test_contradictions_are_strings_and_capped():
    result = normalise_result(
        {"contradictions": ["a", "b", "c", "d", "e", "f", "g", "h"]}, EVIDENCE
    )
    assert len(result["contradictions"]) == 6
    assert all(isinstance(c, str) for c in result["contradictions"])
