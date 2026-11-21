from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Dict, Any


# =============================================================================
# Enums
# =============================================================================

class OutbreakLifecycle(str, Enum):

    EMERGING = "emerging"
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    ACTIVE = "active"
    CONTAINED = "contained"
    RESOLVED = "resolved"
    HISTORICAL = "historical"


class ClaimType(str, Enum):

    SUSPECTED = "suspected"
    OFFICIAL = "official"
    MEDIA_REPORT = "media_report"
    FIELD_REPORT = "field_report"
    SCIENTIFIC = "scientific"
    RUMOR = "rumor"
    UNKNOWN = "unknown"


class SeverityLevel(str, Enum):

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class SpreadRisk(str, Enum):

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"
    # Kept as an alias so older stored documents and the v3 code keep working.
    EXTREME = "critical"


class VerificationStatus(str, Enum):

    UNVERIFIED = "unverified"
    PARTIALLY_VERIFIED = "partially_verified"
    VERIFIED = "verified"
    CONFLICTING = "conflicting"


class AudienceType(str, Enum):

    PUBLIC = "public"
    PRACTITIONER = "practitioner"
    NGO = "ngo"


# =============================================================================
# Coercion — the prompt asks for free-text labels, so map them onto the enums
# instead of letting a bare `except` swallow the difference.
# =============================================================================

def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


CLAIM_TYPE_ALIASES = {
    "official": ClaimType.OFFICIAL,
    "official_report": ClaimType.OFFICIAL,
    "government": ClaimType.OFFICIAL,
    "who": ClaimType.OFFICIAL,
    "media": ClaimType.MEDIA_REPORT,
    "media_report": ClaimType.MEDIA_REPORT,
    "news": ClaimType.MEDIA_REPORT,
    "news_report": ClaimType.MEDIA_REPORT,
    "rumor": ClaimType.RUMOR,
    "rumour": ClaimType.RUMOR,
    "unverified": ClaimType.RUMOR,
    "social_media": ClaimType.RUMOR,
    "field": ClaimType.FIELD_REPORT,
    "field_report": ClaimType.FIELD_REPORT,
    "worker_report": ClaimType.FIELD_REPORT,
    "scientific": ClaimType.SCIENTIFIC,
    "paper": ClaimType.SCIENTIFIC,
    "peer_reviewed": ClaimType.SCIENTIFIC,
    "suspected": ClaimType.SUSPECTED,
    "unknown": ClaimType.UNKNOWN,
    "": ClaimType.UNKNOWN,
}


def coerce_claim_type(value: Any) -> ClaimType:
    key = _norm(value)
    if key in CLAIM_TYPE_ALIASES:
        return CLAIM_TYPE_ALIASES[key]
    try:
        return ClaimType(key)
    except ValueError:
        return ClaimType.UNKNOWN


def coerce_spread_risk(value: Any) -> SpreadRisk:
    key = _norm(value)
    if key in ("extreme", "severe", "pandemic"):
        return SpreadRisk.CRITICAL
    try:
        return SpreadRisk(key)
    except ValueError:
        return SpreadRisk.MODERATE


def coerce_lifecycle(value: Any) -> OutbreakLifecycle:
    key = _norm(value)
    try:
        return OutbreakLifecycle(key)
    except ValueError:
        return OutbreakLifecycle.ACTIVE


def coerce_severity(value: Any) -> SeverityLevel:
    key = _norm(value)
    # The frontend used to speak in alert/advisory/monitoring; accept both.
    if key in ("alert", "severe"):
        return SeverityLevel.CRITICAL
    if key in ("advisory", "warning"):
        return SeverityLevel.HIGH
    if key == "monitoring":
        return SeverityLevel.MODERATE
    try:
        return SeverityLevel(key)
    except ValueError:
        return SeverityLevel.LOW


# =============================================================================
# Evidence Layer
# =============================================================================

@dataclass
class EvidenceDocument:

    source_name: str
    source_url: str

    disease: str
    country: str

    title: str
    extracted_text: str

    source_reputation: float = 0.5

    retrieved_at: datetime = field(
        default_factory=lambda:
        datetime.now(timezone.utc)
    )

    published_at: Optional[datetime] = None

    verification_status: VerificationStatus = (
        VerificationStatus.UNVERIFIED
    )

    srag_session: Optional[str] = None

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )


# =============================================================================
# Intelligence Layer
# =============================================================================

@dataclass
class IntelligenceAnalysis:

    outbreak_detected: bool

    disease: str
    primary_country: str

    lifecycle_stage: OutbreakLifecycle

    claim_type: ClaimType

    confidence_score: float

    estimated_cases: int = 0
    estimated_deaths: int = 0

    spread_risk: SpreadRisk = SpreadRisk.MODERATE

    neighboring_mentions: List[str] = field(
        default_factory=list
    )

    reasoning: str = ""

    ai_summary: str = ""

    contradictions: List[str] = field(
        default_factory=list
    )

    generated_at: datetime = field(
        default_factory=lambda:
        datetime.now(timezone.utc)
    )


# =============================================================================
# Human Intelligence Layer
# =============================================================================

@dataclass
class WorkerReview:

    worker_name: str

    credentials: str

    country: str

    institution: str

    verified: bool

    message: str

    classification_vote: str

    severity_vote: SeverityLevel

    submitted_at: datetime = field(
        default_factory=lambda:
        datetime.now(timezone.utc)
    )


# =============================================================================
# Operational Incident Layer
# =============================================================================

@dataclass
class CanonicalIncident:

    incident_id: str

    disease: str

    region: str

    country: str

    population: int

    estimated_cases: int

    estimated_deaths: int

    severity: SeverityLevel

    spread_risk: SpreadRisk

    lifecycle_stage: OutbreakLifecycle

    claim_type: ClaimType

    confidence_score: float

    verification_status: VerificationStatus

    # Stable identity across runs — disease+country, no date in the key.
    incident_key: str = ""

    trigger: Optional[str] = None

    source_count: int = 0

    source_urls: List[str] = field(
        default_factory=list
    )

    source_names: List[str] = field(
        default_factory=list
    )

    neighboring_mentions: List[str] = field(
        default_factory=list
    )

    ai_summary: str = ""

    reasoning: str = ""

    published_at: Optional[datetime] = None

    evidence_session: Optional[str] = None

    worker_reviews: List[WorkerReview] = field(
        default_factory=list
    )

    created_at: datetime = field(
        default_factory=lambda:
        datetime.now(timezone.utc)
    )

    updated_at: datetime = field(
        default_factory=lambda:
        datetime.now(timezone.utc)
    )

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )

    def to_dict(self):

        return asdict(self)

    def to_doc(self) -> dict:
        """BSON-safe document, enums flattened to their string values."""

        doc = {}

        for key, value in asdict(self).items():
            if isinstance(value, Enum):
                doc[key] = value.value
            else:
                doc[key] = value

        return doc
