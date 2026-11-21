"""
Live sources.

Two kinds of adapter:

  kind="articles" — an index (RSS / JSON API / HTML listing) yields *article*
                    URLs. The pipeline then fetches each article and hands the
                    article text to Gemini. This is where case counts live.

  kind="listing"  — no usable index, so the listing page text itself goes to
                    Gemini. Kept for sources that have no feed.

Every parser is a pure function so it can be tested without the network.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser

from fetch import fetch_bytes, fetch_json, fetch_page, html_to_text

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    """One thing worth showing to Gemini."""

    source_name: str
    url: str
    title: str = ""
    published_at: Optional[datetime] = None
    reputation: float = 0.5
    tier: str = "MEDIUM"
    hint_disease: str = ""
    hint_country: str = ""
    inline_text: str = ""

    def key(self) -> str:
        return self.url.split("?")[0].rstrip("/").lower()


# =============================================================================
# Source registry
# =============================================================================

SOURCES = {
    # ── Feeds and APIs (article-level) ──
    "WHO DON": {
        "kind": "articles",
        "parser": "who_don",
        "url": "https://www.who.int/emergencies/disease-outbreak-news",
        "reputation": 0.98,
        "tier": "HIGH",
        "limit": 8,
    },
    "CDC Travel Notices": {
        "kind": "articles",
        "parser": "rss",
        "url": "https://wwwnc.cdc.gov/travel/rss/notices.xml",
        "reputation": 0.95,
        "tier": "HIGH",
        "limit": 12,
    },
    "ECDC": {
        "kind": "articles",
        "parser": "ecdc",
        "url": "https://www.ecdc.europa.eu/en/threats-and-outbreaks",
        "reputation": 0.94,
        "tier": "HIGH",
        "limit": 8,
    },
    "ReliefWeb": {
        "kind": "articles",
        "parser": "reliefweb",
        "url": "https://api.reliefweb.int/v1/reports",
        "reputation": 0.92,
        "tier": "HIGH",
        "limit": 12,
    },
    "ProMED": {
        "kind": "articles",
        "parser": "promed",
        "url": "https://promedmail.org/promed-posts/",
        "reputation": 0.90,
        "tier": "HIGH",
        "limit": 8,
    },
    "Outbreak News Today": {
        "kind": "articles",
        "parser": "rss",
        "url": "https://outbreaknewstoday.com/feed/",
        "reputation": 0.72,
        "tier": "MEDIUM",
        "limit": 10,
    },

    # ── News corroboration (off by default: it is noisy and costs a Gemini
    #    call per article). Enable with ENABLE_GDELT=1.
    "GDELT": {
        "kind": "articles",
        "parser": "gdelt",
        "url": "https://api.gdeltproject.org/api/v2/doc/doc",
        "reputation": 0.42,
        "tier": "LOW",
        "limit": 6,
        "enabled": bool(os.getenv("ENABLE_GDELT")),
    },

    # ── Listing pages (legacy: text straight to Gemini) ──
    "WHO AFRO": {
        "kind": "listing",
        "url": "https://www.afro.who.int/health-topics/disease-outbreaks/outbreaks-and-other-emergencies-updates",
        "reputation": 0.96,
        "tier": "HIGH",
    },
    "HealthMap": {
        "kind": "listing",
        "url": "https://www.healthmap.org/en/",
        "reputation": 0.78,
        "tier": "MEDIUM",
    },
}

RELIFWEB_FIELDS = [
    "title",
    "url",
    "date.created",
    "country.name",
    "source.name",
    "body",
]


# =============================================================================
# Helpers
# =============================================================================

def _parse_date(value) -> Optional[datetime]:
    """feedparser gives struct_time; ReliefWeb gives ISO strings."""

    if not value:
        return None

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    try:
        import time as _time
        return datetime.fromtimestamp(_time.mktime(value), tz=timezone.utc)
    except Exception:
        return None


def _absolutise(base: str, href: str) -> Optional[str]:
    if not href or href.startswith(("#", "mailto:", "javascript:")):
        return None
    url = urljoin(base, href.strip())
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url


def _links(html: str, base_url: str, fragments, limit: int) -> list[str]:
    """Ordered, de-duplicated absolute links whose path contains a fragment."""

    if not html:
        return []

    found, seen = [], set()

    for match in re.finditer(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        url = _absolutise(base_url, match.group(1))
        if not url:
            continue
        path = urlparse(url).path.lower()
        if not any(frag in path for frag in fragments):
            continue
        if url in seen:
            continue
        seen.add(url)
        found.append(url)
        if len(found) >= limit:
            break

    return found


# =============================================================================
# Parsers (pure)
# =============================================================================

def parse_who_don(html: str, cfg: dict, limit: int) -> list[Candidate]:
    """WHO DON listing → links to individual Disease Outbreak News items."""

    urls = _links(
        html,
        cfg["url"],
        ["/emergencies/disease-outbreak-news/item/"],
        limit,
    )

    return [
        Candidate(
            source_name="WHO DON",
            url=u,
            reputation=cfg["reputation"],
            tier=cfg["tier"],
        )
        for u in urls
    ]


def parse_ecdc(html: str, cfg: dict, limit: int) -> list[Candidate]:
    urls = _links(
        html,
        cfg["url"],
        [
            "/en/disease-outbreaks/",
            "/en/news-events/",
            "/en/threats-and-outbreaks/",
        ],
        limit,
    )

    # The threats page links back to itself in its own nav.
    urls = [u for u in urls if u.rstrip("/") != cfg["url"].rstrip("/")]

    return [
        Candidate(
            source_name="ECDC",
            url=u,
            reputation=cfg["reputation"],
            tier=cfg["tier"],
        )
        for u in urls[:limit]
    ]


def parse_rss(raw: bytes, cfg: dict, limit: int) -> list[Candidate]:
    """RSS/Atom → articles. Used by CDC and Outbreak News Today."""

    feed = feedparser.parse(raw)
    out = []

    for entry in feed.entries[:limit]:
        link = entry.get("link") or ""
        if not link.startswith("http"):
            continue

        summary = entry.get("summary") or entry.get("description") or ""
        inline = html_to_text(summary, 2000) if summary else ""

        out.append(
            Candidate(
                source_name=cfg.get("name", ""),
                url=link,
                title=(entry.get("title") or "").strip(),
                published_at=_parse_date(
                    entry.get("published_parsed") or entry.get("updated_parsed")
                ),
                reputation=cfg["reputation"],
                tier=cfg["tier"],
                inline_text=inline,
            )
        )

    return out


def parse_reliefweb(payload: dict, cfg: dict, limit: int) -> list[Candidate]:
    """ReliefWeb v1 /reports JSON → articles. `body` saves us a fetch."""

    out = []

    for item in (payload or {}).get("data", [])[:limit]:
        fields = item.get("fields") or {}

        url = fields.get("url") or ""
        if not url.startswith("http"):
            continue

        countries = [c.get("name") for c in fields.get("country") or [] if c.get("name")]
        sources = [s.get("name") for s in fields.get("source") or [] if s.get("name")]

        body = fields.get("body") or ""
        inline = html_to_text(body, 8000) if body else ""

        out.append(
            Candidate(
                source_name="ReliefWeb",
                url=url,
                title=(fields.get("title") or "").strip(),
                published_at=_parse_date((fields.get("date") or {}).get("created")),
                reputation=cfg["reputation"],
                tier=cfg["tier"],
                hint_country=countries[0] if countries else "",
                inline_text=inline,
            )
        )

    return out


def parse_promed(raw: bytes, cfg: dict, limit: int) -> list[Candidate]:
    """ProMED has had several feed URLs over the years — try RSS, then HTML."""

    out = parse_rss(raw, {**cfg, "name": "ProMED"}, limit)
    if out:
        return out

    html = raw.decode("utf-8", "replace")
    urls = _links(html, cfg["url"], ["/promed-post/", "/promed-posts/"], limit)

    return [
        Candidate(
            source_name="ProMED",
            url=u,
            reputation=cfg["reputation"],
            tier=cfg["tier"],
        )
        for u in urls
    ]


GDELT_QUERY = (
    '(cholera OR ebola OR mpox OR measles OR dengue OR "yellow fever" OR polio '
    'OR typhoid OR diphtheria OR "avian influenza" OR marburg) '
    '(outbreak OR epidemic OR "cases reported")'
)


def parse_gdelt(payload: dict, cfg: dict, limit: int) -> list[Candidate]:
    """GDELT DOC 2.0 artlist → news articles. Free, no key."""

    out = []

    for item in (payload or {}).get("articles", [])[:limit]:
        url = item.get("url") or ""
        if not url.startswith("http"):
            continue

        # seendate looks like 20261121T120000Z
        published = None
        raw_date = str(item.get("seendate") or "")
        if len(raw_date) >= 15:
            try:
                published = datetime.strptime(raw_date, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                published = None

        out.append(
            Candidate(
                source_name="GDELT",
                url=url,
                title=(item.get("title") or "").strip(),
                published_at=published,
                reputation=cfg["reputation"],
                tier=cfg["tier"],
            )
        )

    return out


PARSERS = {
    "who_don": parse_who_don,
    "ecdc": parse_ecdc,
    "rss": parse_rss,
    "reliefweb": parse_reliefweb,
    "promed": parse_promed,
    "gdelt": parse_gdelt,
}


def slug_hints(url: str) -> tuple[str, str]:
    """CDC URLs encode disease and country: /notices/level2/yellow-fever-ghana"""

    tail = urlparse(url).path.rstrip("/").split("/")[-1]
    parts = [p for p in tail.split("-") if p and not re.fullmatch(r"level\d", p)]

    if len(parts) < 2:
        return "", ""

    country = parts[-1].replace("_", " ").title()
    disease = " ".join(parts[:-1]).title()
    return disease, country


# =============================================================================
# Collectors (network)
# =============================================================================

async def collect_source(client, name: str, cfg: dict) -> list[Candidate]:
    """Fetch one source's index and return candidates. Never raises."""

    if cfg.get("enabled") is False:
        return []

    limit = int(cfg.get("limit", 10))
    kind = cfg.get("kind", "articles")

    try:
        if kind == "listing":
            text = await fetch_page(client, cfg["url"], max_chars=12_000)
            if not text:
                return []
            return [
                Candidate(
                    source_name=name,
                    url=cfg["url"],
                    reputation=cfg["reputation"],
                    tier=cfg["tier"],
                    inline_text=text,
                )
            ]

        parser_name = cfg["parser"]

        if parser_name == "reliefweb":
            params = {
                "appname": "crisiswatch",
                "limit": limit,
                "sort[]": "date:desc",
                "filter[field]": "disaster_type",
                "filter[value]": "EP",
                "fields[include][]": RELIFWEB_FIELDS,
            }
            payload = await fetch_json(client, cfg["url"], params=params)
            if not payload:
                return []
            candidates = parse_reliefweb(payload, cfg, limit)

        elif parser_name == "gdelt":
            params = {
                "query": GDELT_QUERY,
                "mode": "artlist",
                "maxrecords": limit,
                "timespan": "2d",
                "format": "json",
                "sort": "hybridrel",
            }
            payload = await fetch_json(client, cfg["url"], params=params)
            if not payload:
                return []
            candidates = parse_gdelt(payload, cfg, limit)

        elif parser_name in ("rss", "promed"):
            raw = await fetch_bytes(client, cfg["url"])
            if not raw:
                return []
            candidates = PARSERS[parser_name](raw, {**cfg, "name": name}, limit)

        else:
            html = await fetch_bytes(client, cfg["url"])
            if not html:
                return []
            candidates = PARSERS[parser_name](
                html.decode("utf-8", "replace"), cfg, limit
            )

        for c in candidates:
            c.source_name = name
            if not c.title and "cdc.gov" in c.url:
                c.hint_disease, c.hint_country = slug_hints(c.url)

        log.info(f"[Source] {name} → {len(candidates)} candidate(s)")
        return candidates

    except Exception as exc:
        log.warning(f"[Source] {name} failed: {type(exc).__name__}: {exc}")
        return []
