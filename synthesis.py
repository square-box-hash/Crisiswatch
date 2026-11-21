"""
Cross-source synthesis.

Extraction reads one article and produces a claim. This module reads *every*
claim about the same outbreak together and asks Gemini to reconcile them: which
figures agree, which contradict, what the combined picture is.

It deliberately does not let the model invent numbers — the headline counts are
chosen deterministically in assess.py from what sources actually stated. The
model's job is narrative, contradiction-finding and an agreement verdict.
"""

import asyncio
import json
import logging
import re
from typing import Optional

import extract

log = logging.getLogger(__name__)

SYNTHESIS_PROMPT = """
You are a senior epidemiological intelligence analyst reconciling reports about
one outbreak from several independent sources. You brief health ministers.

OUTBREAK: {disease} — {country}

SOURCE REPORTS (each with its URL, date and what that source claimed):

{evidence_block}

═══════════════════════════════════════
YOUR TASK
═══════════════════════════════════════

1. Compare the reports. Where do they agree? Where do they contradict?
2. Identify the single best-supported case and death figures. Use ONLY numbers
   that appear in the reports above. If no report states a figure, use null —
   never 0, and never estimate.
3. State which URL supports the figure you chose.
4. Write a 2-3 sentence briefing for a health minister that reflects the
   combined evidence, not any single source.
5. Name the contradictions explicitly. "Source A says 635, source B says 120"
   is a contradiction; do not average them.

RULES
- Never invent a number, a date or a place.
- Prefer official sources (WHO, CDC, ECDC, ReliefWeb, ProMED) over news.
- If the sources describe genuinely different events, say so in contradictions
  and set same_event to false.

═══════════════════════════════════════
OUTPUT — JSON only, no markdown
═══════════════════════════════════════
{{
  "same_event": true,
  "cases": integer or null,
  "deaths": integer or null,
  "cases_source_url": "the URL that states the figure, or empty",
  "agreement": "agree | partial | conflict",
  "contradictions": ["short, specific statements"],
  "summary": "2-3 sentences for a health minister",
  "reasoning": "how you reconciled the reports, referencing sources",
  "confidence": 0.0 to 1.0
}}
"""

MAX_EXCERPT = 1200


def _fmt_date(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10] if value else "unknown"


def build_evidence_block(evidence: list) -> str:
    """Numbered report summaries, oldest first, with what each one claimed."""

    # Oldest first, undated reports last — a None date must not sort first.
    ordered = sorted(
        evidence,
        key=lambda e: (
            e.get("published_at") is None,
            str(e.get("published_at") or ""),
        ),
    )

    blocks = []

    for i, entry in enumerate(ordered, start=1):
        cases = entry.get("cases")
        deaths = entry.get("deaths")

        figures = []
        figures.append(
            f"cases: {cases}" if cases is not None else "cases: not stated"
        )
        figures.append(
            f"deaths: {deaths}" if deaths is not None else "deaths: not stated"
        )

        excerpt = (entry.get("excerpt") or "").strip()
        if len(excerpt) > MAX_EXCERPT:
            excerpt = excerpt[:MAX_EXCERPT] + "…"

        blocks.append(
            f"[{i}] {entry.get('source_name')} ({entry.get('tier')} reputation)\n"
            f"    URL: {entry.get('url')}\n"
            f"    Published: {_fmt_date(entry.get('published_at'))}\n"
            f"    Reported — {', '.join(figures)}\n"
            f"    Text: {excerpt or '(no excerpt)'}"
        )

    return "\n\n".join(blocks)


def build_prompt(disease: str, country: str, evidence: list) -> str:
    return (
        SYNTHESIS_PROMPT
        .replace("{disease}", disease)
        .replace("{country}", country)
        .replace("{evidence_block}", build_evidence_block(evidence))
    )


FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_object(raw: str) -> Optional[dict]:
    if not raw:
        return None

    text = FENCE.sub("", raw.strip()).strip()

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    return value if isinstance(value, dict) else None


def _coerce_int(value) -> Optional[int]:
    """null stays null. A number is a number. Garbage is dropped."""

    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 100_000_000:
        return None
    return number


def normalise_result(result: dict, evidence: list) -> dict:
    """Keep only what we trust: numbers that trace to a real source URL."""

    urls = {str(e.get("url") or "") for e in evidence}

    cases = _coerce_int(result.get("cases"))
    deaths = _coerce_int(result.get("deaths"))

    claimed_url = str(result.get("cases_source_url") or "").strip()

    # A figure must be attributable to one of the sources we actually read.
    if cases is not None and claimed_url not in urls:
        claimed_url = ""

    agreement = str(result.get("agreement") or "").lower()
    if agreement not in ("agree", "partial", "conflict"):
        agreement = "partial"

    contradictions = result.get("contradictions") or []
    if not isinstance(contradictions, list):
        contradictions = [str(contradictions)]

    try:
        confidence = float(result.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "same_event": bool(result.get("same_event", True)),
        "cases": cases,
        "deaths": deaths,
        "cases_source_url": claimed_url,
        "agreement": agreement,
        "contradictions": [str(c)[:400] for c in contradictions][:6],
        "summary": str(result.get("summary") or "")[:1200],
        "reasoning": str(result.get("reasoning") or "")[:2000],
        "confidence": max(0.0, min(confidence, 1.0)),
    }


async def synthesise(disease: str, country: str, evidence: list) -> Optional[dict]:
    """One Gemini call over all evidence for an outbreak. None on failure."""

    if len(evidence) < 2:
        return None

    prompt = build_prompt(disease, country, evidence)

    try:
        raw = await asyncio.to_thread(extract._call_gemini, prompt)
    except Exception as exc:
        log.warning(f"[Synthesis] {disease}/{country} → {type(exc).__name__}: {exc}")
        return None

    result = parse_object(raw)

    if result is None:
        log.warning(
            f"[Synthesis] {disease}/{country} → unparseable: {raw[:160]!r}"
        )
        return None

    normalised = normalise_result(result, evidence)

    log.info(
        f"[Synthesis] {disease}/{country} → {normalised['agreement']}, "
        f"{len(normalised['contradictions'])} contradiction(s)"
    )

    return normalised
