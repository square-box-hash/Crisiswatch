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


class SeverityLevel(str, Enum):

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class SpreadRisk(str, Enum):

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    EXTREME = "extreme"


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

    trigger: Optional[str] = None

    source_count: int = 0

    source_urls: List[str] = field(
        default_factory=list
    )

    neighboring_mentions: List[str] = field(
        default_factory=list
    )

    ai_summary: str = ""

    reasoning: str = ""

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
