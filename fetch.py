"""HTTP fetching + HTML→text. No Gemini, no Mongo — safe to unit test."""

import asyncio
import logging
import re
from typing import Optional

import httpx

import diagnostics

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; CrisisWatch/5.0; "
    "+https://github.com/square-box-hash/Crisiswatch)"
)

# Tags whose contents are never article text.
DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside|form|svg)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ",
    "&#39;": "'", "&apos;": "'", "&quot;": '"', "&mdash;": "—",
    "&ndash;": "–", "&rsquo;": "'", "&lsquo;": "'", "&hellip;": "…",
}


def html_to_text(html: str, max_chars: int = 12_000) -> str:
    """Strip a page down to readable text. Deliberately dependency-free."""

    if not html:
        return ""

    text = DROP_BLOCKS.sub(" ", html)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)

    for entity, char in HTML_ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text[:max_chars]


async def fetch_page(
    client: httpx.AsyncClient,
    url: str,
    max_chars: int = 12_000,
    timeout: float = 30.0,
    record=None,
) -> Optional[str]:
    """Fetch a page and return clean text, or None. Never raises."""

    try:
        resp = await client.get(url, timeout=timeout)
    except Exception as exc:
        log.warning(f"[Fetch] {url} → {type(exc).__name__}: {exc}")
        if record is not None:
            record.fail_exc("fetch", url, exc, url=url)
        return None

    if resp.status_code != 200:
        log.warning(f"[Fetch] {url} → HTTP {resp.status_code}")
        if record is not None:
            record.fail_status("fetch", url, resp.status_code, resp.text[:200], url=url)
        return None

    text = html_to_text(resp.text, max_chars)

    if len(text) < 200:
        log.warning(f"[Fetch] {url} → only {len(text)} chars of text, skipping")
        if record is not None:
            record.fail(
                "fetch", url, diagnostics.EMPTY,
                f"only {len(text)} chars of readable text (likely JS-rendered)",
                url=url,
            )
        return None

    return text


async def fetch_bytes(
    client: httpx.AsyncClient,
    url: str,
    timeout: float = 30.0,
    record=None,
) -> Optional[bytes]:
    try:
        resp = await client.get(url, timeout=timeout)
    except Exception as exc:
        log.warning(f"[Fetch] {url} → {type(exc).__name__}: {exc}")
        if record is not None:
            record.fail_exc("fetch", url, exc, url=url)
        return None

    if resp.status_code != 200:
        log.warning(f"[Fetch] {url} → HTTP {resp.status_code}")
        if record is not None:
            record.fail_status("fetch", url, resp.status_code, resp.text[:200], url=url)
        return None

    if not resp.content:
        if record is not None:
            record.fail("fetch", url, diagnostics.EMPTY, "empty body", url=url)
        return None

    return resp.content


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    params: Optional[dict] = None,
    timeout: float = 30.0,
    record=None,
) -> Optional[dict]:
    try:
        resp = await client.get(url, params=params, timeout=timeout)
    except Exception as exc:
        log.warning(f"[Fetch] {url} → {type(exc).__name__}: {exc}")
        if record is not None:
            record.fail_exc("fetch", url, exc, url=url)
        return None

    if resp.status_code != 200:
        # Log the body: a 400 from an API tells you exactly which param is wrong.
        log.warning(
            f"[Fetch] {url} → HTTP {resp.status_code}: {resp.text[:300]}"
        )
        if record is not None:
            record.fail_status("fetch", url, resp.status_code, resp.text[:300], url=url)
        return None

    try:
        return resp.json()
    except Exception as exc:
        log.warning(f"[Fetch] {url} → bad JSON: {exc}")
        if record is not None:
            record.fail_exc("fetch", url, exc, status=200, url=url)
        return None


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    )


async def gather_limited(coros, limit: int = 5):
    """Run coroutines with a concurrency cap, preserving order."""

    semaphore = asyncio.Semaphore(limit)

    async def run(coro):
        async with semaphore:
            try:
                return await coro
            except Exception as exc:
                log.warning(f"[Gather] {type(exc).__name__}: {exc}")
                return None

    return await asyncio.gather(*(run(c) for c in coros))
