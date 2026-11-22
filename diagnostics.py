"""
Structured diagnostics.

Every failure gets three things: the stage it happened in, the subject it
happened to (a URL, a source, a model), and a kind that says what went wrong.
That turns "0 candidates" into "403 from ecdc.europa.eu" without reading code.

The vocabulary is deliberately small — if a kind does not tell you what to do
next, it is the wrong kind.
"""

import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# What went wrong.
NO_KEY = "no_key"                 # credential missing
AUTH = "auth"                     # 401 / 403 — wrong or expired key
RATE_LIMIT = "rate_limit"         # 429 / quota
UNAVAILABLE = "unavailable"       # 5xx from the remote
NOT_FOUND = "not_found"           # 404 — the URL moved
TIMEOUT = "timeout"               # no answer in time
DNS = "dns"                       # hostname did not resolve
CONNECTION = "connection"         # refused / reset / TLS
HTTP_ERROR = "http_error"         # any other non-200
PARSE = "parse"                   # response arrived, could not be understood
EMPTY = "empty"                   # response arrived, nothing in it
NO_MATCH = "no_match"             # content arrived, none of it about our case
BLOCKED = "blocked"               # blocked by policy / robots / geo
DISABLED = "disabled"             # switched off by configuration
QUOTA = "quota"                   # free-tier allowance exhausted for the day
UNKNOWN = "unknown"

# Which kinds are worth retrying later, and which are pointless.
RETRYABLE = {RATE_LIMIT, UNAVAILABLE, TIMEOUT, CONNECTION, DNS}

# Hosts where a 403 means "your key", not "your client is blocked".
API_HOSTS = ("googleapis.com", "tavily.com", "reliefweb.int")


def looks_like_daily_quota(message: str) -> bool:
    """A per-day allowance that retrying cannot fix."""

    text = (message or "").lower()
    return any(
        marker in text
        for marker in ("perday", "per day", "free_tier_requests", "daily limit")
    )

# A hint, per kind, that tells the reader what to do about it.
ADVICE = {
    NO_KEY: "set the credential in the environment",
    AUTH: "the key is wrong, expired, or lacks permission for this endpoint",
    RATE_LIMIT: "quota exhausted or too many requests — slow down or raise the limit",
    UNAVAILABLE: "the remote is down — transient, the next run may succeed",
    NOT_FOUND: "the URL moved — the source config needs updating",
    TIMEOUT: "the remote did not answer in time — check connectivity or raise the timeout",
    DNS: "the hostname does not resolve — check the URL, or whether the service exists",
    CONNECTION: "the connection was refused or reset — network or TLS problem",
    HTTP_ERROR: "unexpected status — read the message for the remote's own words",
    PARSE: "the response shape changed — the parser needs updating",
    EMPTY: "the request worked but returned nothing — check the query or the feed",
    NO_MATCH: "results came back but none matched this outbreak — widen the query",
    BLOCKED: "the remote refused this client — user agent, geo, or robots",
    DISABLED: "switched off — enable the flag if you want this source",
    QUOTA: "the daily free-tier allowance is gone — wait for the reset (midnight Pacific) or reduce calls per run",
    UNKNOWN: "read the message",
}

MAX_FAILURES = 60
MAX_MESSAGE = 400


@dataclass
class Failure:
    stage: str
    subject: str
    kind: str
    message: str = ""
    status: Optional[int] = None
    url: Optional[str] = None

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    def line(self) -> str:
        where = f" ({self.url})" if self.url and self.url != self.subject else ""
        code = f" [{self.status}]" if self.status else ""
        return f"{self.stage}/{self.kind}: {self.subject}{code}{where} — {self.message}"


def classify(status: Optional[int] = None, exc: Optional[BaseException] = None,
             message: str = "") -> str:
    """Map an HTTP status or an exception onto the vocabulary above."""

    text = (message or "").lower()

    # A 429 usually arrives as an exception, so read the body too.
    if "429" in text or "resource_exhausted" in text or "quota exceeded" in text:
        return QUOTA if looks_like_daily_quota(message) else RATE_LIMIT

    if status is not None:
        if status == 401:
            return AUTH
        if status == 403:
            # A keyed API refusing us is a credential problem; any other site
            # refusing us is bot protection, which is not something a key fixes.
            if any(host in (message or "") for host in API_HOSTS):
                return AUTH
            return BLOCKED
        if status in (202, 203):
            # Accepted-but-withheld: bot challenge pages do this.
            return BLOCKED
        if status == 404:
            return NOT_FOUND
        if status == 429:
            return RATE_LIMIT
        if 500 <= status < 600:
            return UNAVAILABLE
        if status >= 400:
            return HTTP_ERROR
        return UNKNOWN

    if exc is not None:
        name = type(exc).__name__.lower()
        text = f"{type(exc).__name__}: {exc}".lower()

        if "timeout" in name or "timed out" in text:
            return TIMEOUT
        if "gaierror" in name or "nxdomain" in text or "name does not exist" in text:
            return DNS
        if "connect" in name or "ssl" in name or "certificate" in name:
            return CONNECTION
        if "json" in name and "decode" in text:
            return PARSE
        if "configurationerror" in name:
            return DNS
        return UNKNOWN

    return UNKNOWN


class Recorder:
    """Collects failures for one run. Never raises, never blocks anything."""

    def __init__(self, max_failures: int = MAX_FAILURES):
        self.failures: list[Failure] = []
        self.max_failures = max_failures
        self.dropped = 0
        self.counts: dict[str, int] = {}

    def _add(self, failure: Failure) -> None:
        self.counts[failure.kind] = self.counts.get(failure.kind, 0) + 1

        if len(self.failures) >= self.max_failures:
            self.dropped += 1
            return

        failure.message = (failure.message or "")[:MAX_MESSAGE]
        self.failures.append(failure)

        log.debug(f"[diag] {failure.line()}")

    def fail(self, stage: str, subject: str, kind: str = UNKNOWN,
             message: str = "", status: Optional[int] = None,
             url: Optional[str] = None) -> Failure:
        failure = Failure(
            stage=stage, subject=subject, kind=kind,
            message=message, status=status, url=url,
        )
        self._add(failure)
        return failure

    def fail_exc(self, stage: str, subject: str, exc: BaseException,
                 status: Optional[int] = None, url: Optional[str] = None) -> Failure:
        return self.fail(
            stage=stage,
            subject=subject,
            kind=classify(status=status, exc=exc),
            message=f"{type(exc).__name__}: {exc}",
            status=status,
            url=url,
        )

    def fail_status(self, stage: str, subject: str, status: int, body: str = "",
                    url: Optional[str] = None) -> Failure:
        return self.fail(
            stage=stage,
            subject=subject,
            kind=classify(status=status, message=body),
            message=(body or "")[:MAX_MESSAGE],
            status=status,
            url=url,
        )

    # ── reporting ────────────────────────────────────────────

    def by_stage(self) -> dict:
        out: dict[str, dict] = {}
        for failure in self.failures:
            bucket = out.setdefault(failure.stage, {"count": 0, "kinds": {}})
            bucket["count"] += 1
            bucket["kinds"][failure.kind] = bucket["kinds"].get(failure.kind, 0) + 1
        return out

    def advice(self) -> list:
        seen, out = set(), []
        for failure in self.failures:
            if failure.kind in seen:
                continue
            seen.add(failure.kind)
            out.append({"kind": failure.kind, "advice": ADVICE.get(failure.kind, "")})
        return out

    def headline(self) -> str:
        """One line for the run log: 'fetch/timeout x3, research/rate_limit x1'."""

        if not self.failures and not self.dropped:
            return "no failures"

        pairs: dict[str, int] = {}
        for failure in self.failures:
            key = f"{failure.stage}/{failure.kind}"
            pairs[key] = pairs.get(key, 0) + 1

        parts = [f"{key} ×{count}" for key, count in sorted(pairs.items())]
        if self.dropped:
            parts.append(f"+{self.dropped} more")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {
            "counts": dict(self.counts),
            "by_stage": self.by_stage(),
            "failures": [asdict(f) for f in self.failures],
            "dropped": self.dropped,
            "advice": self.advice(),
            "headline": self.headline(),
        }


# A recorder that accepts everything and stores nothing, for callers that do not
# care (tests, one-off scripts).
class NullRecorder(Recorder):
    def _add(self, failure: Failure) -> None:
        return
