"""Data models for BeHive research results."""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class Claim:
    """A structured factual claim extracted from research."""
    text: str
    confidence: float
    quality_score: float
    source_url: str = ""
    claim_type: str = "intelligence"
    extracted_at: Optional[datetime] = None
    enriched: bool = False
    
    def __str__(self):
        return f"[{self.quality_score:.2f}] {self.text}"


@dataclass
class Entity:
    """A named entity discovered during research."""
    name: str
    entity_type: str  # person, org, location, technology, product, amount
    confidence: float = 0.8
    mentions: int = 1
    
    def __str__(self):
        return f"{self.name} ({self.entity_type})"


@dataclass
class ResearchResult:
    """Complete result of a research mission."""
    topic: str
    mission_id: str
    claims: list[Claim] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    report: str = ""
    
    # Metrics
    sources_processed: int = 0
    duration_seconds: float = 0.0
    avg_quality: float = 0.0
    
    # Status
    status: str = "pending"  # pending, running, done, error
    error: Optional[str] = None
    
    @property
    def excellent_claims(self) -> list[Claim]:
        """Claims with quality >= 0.80."""
        return [c for c in self.claims if c.quality_score >= 0.80]
    
    @property
    def summary(self) -> str:
        """One-line summary."""
        return (
            f"{len(self.claims)} claims (avg {self.avg_quality:.2f}), "
            f"{len(self.entities)} entities, "
            f"{self.sources_processed} sources in {self.duration_seconds:.0f}s"
        )
    
    def to_dict(self) -> dict:
        """Serialize for JSON/API responses."""
        return {
            "topic": self.topic,
            "mission_id": self.mission_id,
            "status": self.status,
            "claims": [
                {"text": c.text, "confidence": c.confidence, 
                 "quality_score": c.quality_score, "source_url": c.source_url,
                 "type": c.claim_type, "enriched": c.enriched}
                for c in self.claims
            ],
            "entities": [
                {"name": e.name, "type": e.entity_type, 
                 "confidence": e.confidence, "mentions": e.mentions}
                for e in self.entities
            ],
            "report": self.report,
            "metrics": {
                "sources_processed": self.sources_processed,
                "duration_seconds": self.duration_seconds,
                "avg_quality": self.avg_quality,
                "total_claims": len(self.claims),
                "excellent_claims": len(self.excellent_claims),
            },
            "error": self.error,
        }
