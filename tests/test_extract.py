"""Gemini response handling and item validation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extract import parse_model_json, validate_item  # noqa: E402


def test_plain_array():
    assert parse_model_json('[{"disease": "Cholera"}]') == [{"disease": "Cholera"}]


def test_fenced_array():
    raw = '```json\n[{"disease": "Mpox"}]\n```'
    assert parse_model_json(raw) == [{"disease": "Mpox"}]


def test_single_object_is_wrapped():
    raw = '{"disease": "Ebola", "primary_country": "Uganda"}'
    assert parse_model_json(raw) == [{"disease": "Ebola", "primary_country": "Uganda"}]


def test_object_with_outbreaks_key():
    raw = '{"outbreaks": [{"disease": "Cholera"}]}'
    assert parse_model_json(raw) == [{"disease": "Cholera"}]


def test_prose_around_the_array():
    raw = 'Here are the results:\n[{"disease": "Measles"}]\nHope this helps.'
    assert parse_model_json(raw) == [{"disease": "Measles"}]


def test_empty_array():
    assert parse_model_json("[]") == []


def test_garbage_returns_none():
    assert parse_model_json("I could not find any outbreaks on this page.") is None
    assert parse_model_json("") is None


def test_valid_item_passes():
    ok, reason = validate_item(
        {
            "outbreak_detected": True,
            "disease": "Cholera",
            "primary_country": "Zambia",
            "estimated_cases": 120,
            "estimated_deaths": 4,
            "confidence": 0.72,
        }
    )
    assert ok, reason


def test_missing_country_is_rejected():
    ok, reason = validate_item({"disease": "Cholera", "primary_country": ""})
    assert not ok
    assert "primary_country" in reason


def test_detected_false_is_rejected():
    ok, reason = validate_item(
        {"outbreak_detected": False, "disease": "Cholera", "primary_country": "Zambia"}
    )
    assert not ok
    assert "outbreak_detected" in reason


def test_string_case_count_is_rejected():
    ok, reason = validate_item(
        {
            "disease": "Cholera",
            "primary_country": "Zambia",
            "estimated_cases": "120 cases",
            "confidence": 0.7,
        }
    )
    assert not ok
    assert "estimated_cases" in reason


def test_confidence_out_of_range_is_rejected():
    ok, reason = validate_item(
        {"disease": "Cholera", "primary_country": "Zambia", "confidence": 95}
    )
    assert not ok
    assert "confidence" in reason


def test_none_case_count_is_zeroed_not_rejected():
    item = {
        "disease": "Cholera",
        "primary_country": "Zambia",
        "estimated_cases": None,
        "confidence": 0.6,
    }
    ok, reason = validate_item(item)
    assert ok, reason
    assert item["estimated_cases"] == 0
