"""
Gemini extraction.

One call per article. The model reads the article and returns structured JSON;
everything here is about making that call observable — if the model name is
wrong, the key is dead or the quota is out, the reason lands in the run log
instead of an empty list.
"""

import asyncio
import json
import logging
import os
import re
from typing import Optional

import diagnostics

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

_client = None
_sdk = None


def init_client() -> Optional[object]:
    """Idempotent. Returns the client, or None with a logged reason."""

    global _client, _sdk

    if _client is not None or _sdk == "none":
        return _client

    if not GEMINI_API_KEY:
        log.error("[Gemini] GEMINI_API_KEY is not set — extraction disabled")
        _sdk = "none"
        return None

    try:
        from google import genai as _genai
        _client = _genai.Client(api_key=GEMINI_API_KEY)
        _sdk = "genai"
        log.info(f"[Gemini] client ready (google-genai), model={GEMINI_MODEL}")
        return _client
    except ImportError:
        pass

    try:
        import google.generativeai as _legacy
        _legacy.configure(api_key=GEMINI_API_KEY)
        _client = _legacy
        _sdk = "legacy"
        log.info(f"[Gemini] client ready (google-generativeai), model={GEMINI_MODEL}")
        return _client
    except ImportError:
        log.error("[Gemini] no SDK installed — pip install google-genai")
        _sdk = "none"
        return None


EXTRACTION_PROMPT = """
You are a senior epidemiological intelligence analyst at a global health
surveillance organisation. You have 20 years of experience reading WHO, CDC,
ECDC, ProMED and ReliefWeb reports, and you brief government health ministers.

Read the article below and extract every ACTIVE disease outbreak it reports,
as structured JSON.

═══════════════════════════════════════
REASONING PROCESS — follow this exactly
═══════════════════════════════════════

Step 1 — SCAN: read the whole article. List every disease, place and number.
Step 2 — FILTER: drop anything historical, resolved, preparedness-only,
         vaccination campaign, or animal-only.
Step 3 — DEDUPLICATE: if one outbreak covers several countries, emit ONE entry
         for the most affected country and list the rest in neighbouring_mentions.
Step 4 — EXTRACT NUMBERS: use exact figures. "635 confirmed cases" → 635.
         "dozens" → 40. "hundreds" → 200.
         If the article states NO figure, return null. Never return 0 as a
         stand-in for "unknown" — 0 means the source said zero, which is rare
         and specific.
Step 5 — CLASSIFY: lifecycle stage, spread risk, confidence, claim type.
Step 6 — SUMMARISE: 2-3 sentences as if briefing a health minister.

═══════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════

HUMAN CASES ONLY
- H5N1 in dairy herds, poultry or wild birds is NOT a human case count.
- "1,000 herds affected" + "71 human cases" → estimated_cases: 71
- Animal detections are context, never the case count.

EXACT NUMBERS
- "635 confirmed cases" → 635 · "two deaths" → 2 · "dozens" → 40
- Never return 0 when the article states a number.
- No figure anywhere → null. A travel advisory that says "yellow fever is
  present in Ghana" states no count: cases must be null, not 0.

DISEASE NAME NORMALISATION
- Bundibugyo virus → "Ebola (Bundibugyo)"
- Monkeypox / Mpox / Clade I / Clade II → "Mpox"
- Avian influenza / H5N1 / bird flu → "H5N1 Avian Influenza"
- COVID-19 / SARS-CoV-2 → "COVID-19"
- Use the most widely recognised name.

ONE ENTRY PER OUTBREAK
- Same disease across several countries in one outbreak → one entry, primary
  country = most affected, others in neighbouring_mentions.

CONFIDENCE
- WHO DON with case counts: 0.90-0.98
- CDC / ECDC advisory: 0.85-0.95
- ReliefWeb situation report: 0.80-0.92
- News article with named sources: 0.65-0.78
- Vague mention, no source: 0.30-0.50
- Never give 0.95+ to a vague mention.

LIFECYCLE STAGE
- emerging: first reports, unconfirmed, investigation ongoing
- suspected: cases reported, lab confirmation pending
- confirmed: lab confirmed, limited spread
- active: confirmed, ongoing transmission, response underway
- contained: cases declining, response controlling spread
- resolved: no new cases for 2+ incubation periods
- historical: past outbreak being referenced

SPREAD RISK
- low: localised, contained, no cross-border risk
- moderate: some spread potential, neighbours at risk
- high: multi-country spread occurring or imminent
- critical: pandemic potential, global spread risk

CLAIM TYPE — use exactly one of:
official | media | rumor | unknown

═══════════════════════════════════════
EXAMPLES
═══════════════════════════════════════

EXAMPLE 1 — human vs animal counts
"Annex: H5N1 has affected over 1,000 dairy herds across 17 US states. As of
May 2026, 71 human cases have been confirmed, primarily in farmworkers."
{"disease":"H5N1 Avian Influenza","primary_country":"United States",
 "estimated_cases":71,"estimated_deaths":0,"spread_risk":"moderate",
 "confidence":0.88,"claim_type":"official",
 "reasoning":"71 human cases stated; 1,000 herds is animal data, ignored."}

EXAMPLE 2 — exact figures
"The Ebola outbreak in DRC caused by Bundibugyo virus has reached 635
confirmed cases including 127 deaths. Uganda has reported imported cases."
{"disease":"Ebola (Bundibugyo)","primary_country":"Democratic Republic of the Congo",
 "neighboring_mentions":["Uganda"],"estimated_cases":635,"estimated_deaths":127,
 "spread_risk":"high","confidence":0.95,"claim_type":"official",
 "lifecycle_stage":"active",
 "reasoning":"635 cases and 127 deaths stated; Uganda listed as neighbour."}

EXAMPLE 3 — vague text, no numbers
"Cholera cases have been reported in Zambia following flooding."
{"disease":"Cholera","primary_country":"Zambia","estimated_cases":null,
 "spread_risk":"moderate","confidence":0.62,"lifecycle_stage":"emerging",
 "claim_type":"media","reasoning":"No case count given; flooding suggests risk."}

EXAMPLE 4 — not an outbreak
"WHO recommends countries prepare for a potential Marburg outbreak. No cases
currently reported."
[]

═══════════════════════════════════════
SOURCE CONTEXT
═══════════════════════════════════════
Source: {source_name}
URL: {url}
Title: {title}
Published: {published}
Reputation tier: {tier}
{hints}

Article content:
{page_text}

═══════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════
Return ONLY a valid JSON array. No markdown, no preamble.
If nothing qualifies, return exactly: []

Each item:
{
  "outbreak_detected": true,
  "disease": "canonical disease name",
  "primary_country": "most affected country",
  "neighboring_mentions": ["other countries"],
  "claim_type": "official | media | rumor | unknown",
  "lifecycle_stage": "emerging | suspected | confirmed | active | contained | resolved | historical",
  "confidence": 0.0 to 1.0,
  "estimated_cases": integer human cases, or null if no figure is stated,
  "estimated_deaths": integer human deaths, or null if no figure is stated,
  "spread_risk": "low | moderate | high | critical",
  "summary": "2-3 sentence briefing for a health minister",
  "reasoning": "step by step, quoting the article"
}
"""


def build_prompt(candidate) -> str:
    hints = ""
    if getattr(candidate, "hint_disease", ""):
        hints += f"Likely disease (from URL): {candidate.hint_disease}\n"
    if getattr(candidate, "hint_country", ""):
        hints += f"Likely country (from URL): {candidate.hint_country}\n"

    published = (
        candidate.published_at.strftime("%Y-%m-%d")
        if getattr(candidate, "published_at", None)
        else "unknown"
    )

    return (
        EXTRACTION_PROMPT
        .replace("{source_name}", candidate.source_name)
        .replace("{url}", candidate.url)
        .replace("{title}", candidate.title or "unknown")
        .replace("{published}", published)
        .replace("{tier}", candidate.tier)
        .replace("{hints}", hints)
        .replace("{page_text}", candidate.text)
    )


# =============================================================================
# Response parsing (pure — unit tested)
# =============================================================================

FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_model_json(raw: str) -> Optional[list]:
    """Tolerant JSON parse: fences, a single object, or prose around an array."""

    if not raw:
        return None

    text = FENCE.sub("", raw.strip()).strip()

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # Grab the outermost array or object and try once more.
        array = re.search(r"\[.*\]", text, re.DOTALL)
        obj = re.search(r"\{.*\}", text, re.DOTALL)
        snippet = array.group(0) if array else (obj.group(0) if obj else None)
        if not snippet:
            return None
        try:
            value = json.loads(snippet)
        except json.JSONDecodeError:
            return None

    if isinstance(value, dict):
        # A single object, or {"outbreaks": [...]}
        for key in ("outbreaks", "data", "results", "items"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]

    return value if isinstance(value, list) else None


def validate_item(item: dict) -> tuple[bool, str]:
    """Reject what would poison the store, with a reason for the log."""

    if not isinstance(item, dict):
        return False, "not an object"

    if item.get("outbreak_detected") is False:
        return False, "outbreak_detected=false"

    disease = str(item.get("disease") or "").strip()
    country = str(item.get("primary_country") or "").strip()

    if not disease:
        return False, "missing disease"
    if not country:
        return False, "missing primary_country"
    if len(disease) > 80:
        return False, f"disease too long ({len(disease)} chars)"
    if len(country) > 80:
        return False, f"country too long ({len(country)} chars)"

    for field_name in ("estimated_cases", "estimated_deaths"):
        value = item.get(field_name)
        # None / "" means the source stated no figure. That is not an error and
        # it must stay None — turning it into 0 invents a fact.
        if value is None or value == "":
            item[field_name] = None
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            return False, f"{field_name} not an integer: {value!r}"
        if number < 0 or number > 100_000_000:
            return False, f"{field_name} out of range: {number}"
        item[field_name] = number

    try:
        confidence = float(item.get("confidence", 0.5))
    except (TypeError, ValueError):
        return False, f"confidence not a number: {item.get('confidence')!r}"

    if not 0.0 <= confidence <= 1.0:
        return False, f"confidence out of range: {confidence}"

    return True, ""


# =============================================================================
# Gemini call
# =============================================================================

def _call_gemini(prompt: str) -> str:
    """Blocking call, run in a thread. Raises with the provider's own message."""

    client = init_client()
    if client is None:
        raise RuntimeError("Gemini client unavailable (no API key or SDK)")

    if _sdk == "genai":
        try:
            from google.genai import types as _types
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=_types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )
        except TypeError:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
    else:
        response = client.generate_content(prompt)

    text = getattr(response, "text", None)

    if not text:
        candidates = getattr(response, "candidates", None)
        if candidates:
            try:
                text = candidates[0].content.parts[0].text
            except Exception:
                text = None

    return (text or "").strip()


async def extract_from_candidate(candidate, attempts: int = 3, record=None) -> tuple[list, str]:
    """
    Returns (items, error). `error` is a human-readable reason when the call
    itself failed — it goes into the run log.
    """

    prompt = build_prompt(candidate)
    last_error = ""

    for attempt in range(attempts):
        try:
            raw = await asyncio.to_thread(_call_gemini, prompt)
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            message = str(exc)

            retryable = any(
                token in message
                for token in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED",
                              "overloaded", "timeout", "Timeout")
            )

            if not retryable or attempt == attempts - 1:
                log.warning(f"[Gemini] {candidate.url} → {last_error}")
                if record is not None:
                    kind = diagnostics.classify(exc=exc)
                    if "api key" in message.lower() or "unavailable" in message.lower():
                        kind = diagnostics.NO_KEY
                    record.fail(
                        "extract", candidate.source_name, kind,
                        last_error, url=candidate.url,
                    )
                return [], last_error

            wait = 4 * (attempt + 1)
            log.info(f"[Gemini] retryable error, waiting {wait}s ({attempt + 1}/{attempts})")
            await asyncio.sleep(wait)
    else:
        return [], last_error or "no response"

    items = parse_model_json(raw)

    if items is None:
        snippet = raw[:200].replace("\n", " ")
        log.warning(f"[Gemini] {candidate.url} → unparseable response: {snippet}")
        if record is not None:
            record.fail(
                "extract", candidate.source_name, diagnostics.PARSE,
                f"model returned text that is not JSON: {snippet}",
                url=candidate.url,
            )
        return [], f"unparseable JSON response: {snippet}"

    valid = []
    for item in items:
        ok, reason = validate_item(item)
        if ok:
            valid.append(item)
        else:
            log.info(f"[Gemini] dropped item from {candidate.url}: {reason}")

    log.info(f"[Gemini] {candidate.source_name} → {len(valid)}/{len(items)} item(s)")
    return valid, ""
