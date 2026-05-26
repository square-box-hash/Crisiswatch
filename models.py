from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from enum import Enum

class OutbreakClassification(Enum):
    REGULAR = "regular"      # known seasonal/trigger pattern
    NOVEL = "novel"          # new, no matching history
    PENDING = "pending"      # awaiting health worker review

class SeverityLevel(Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"

class AudienceType(Enum):
    PUBLIC = "public"
    PRACTITIONER = "practitioner"
    NGO = "ngo"

@dataclass
class ClusterSignal:
    disease: str
    region: str
    country: str
    cases: int
    deaths: int
    population: int
    case_rate: float          # cases per 100k population
    anomaly_score: float      # how unusual vs historical baseline
    trigger: Optional[str]    # "monsoon", "drought", "flood" or None
    source_urls: list[str]
    detected_at: datetime = field(default_factory=datetime.utcnow)

@dataclass
class OutbreakAlert:
    disease: str
    region: str
    country: str
    severity: SeverityLevel
    classification: OutbreakClassification
    ai_assessment: str
    spread_prediction: dict
    safety_advice: dict       # keyed by AudienceType
    cluster_signal: ClusterSignal
    version: int = 1
    worker_messages: list = field(default_factory=list)
    ai_summary: str = ""
    confidence_score: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

@dataclass  
class WorkerMessage:
    worker_name: str
    credentials: str
    country: str
    institution: str
    verified: bool
    message: str
    classification_vote: OutbreakClassification
    severity_vote: SeverityLevel
    submitted_at: datetime = field(default_factory=datetime.utcnow)