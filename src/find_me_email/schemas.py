from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, EmailStr, Field, HttpUrl


class Confidence(str, Enum):
    HIGH = "high"          # Direct DB match, SMTP-verified
    MEDIUM = "medium"      # Direct DB match, unverified
    LOW = "low"            # Pattern guess, SMTP-verified
    SPECULATIVE = "speculative"  # Pattern guess, unverified — DO NOT send without checking

    @property
    def rank(self) -> int:
        return _CONFIDENCE_RANK[self]


_CONFIDENCE_RANK = {
    Confidence.HIGH: 0,
    Confidence.MEDIUM: 1,
    Confidence.LOW: 2,
    Confidence.SPECULATIVE: 3,
}


class Person(BaseModel):
    """Generic person input. Only `row_id` is required; everything else is optional context."""
    row_id: str
    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    linkedin_url: HttpUrl | None = None
    company: str | None = None
    school: str | None = None
    school_domain: str | None = None  # e.g., "mit.edu"
    title: str | None = None
    location: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class EmailCandidate(BaseModel):
    email: str  # not EmailStr — guesses may not parse strictly
    confidence: Confidence
    source_provider: str
    verified: bool = False
    verification_method: str | None = None  # smtp, dns, hunter, etc.
    notes: str = ""
    cost_usd: float = 0.0
    raw: dict[str, Any] | None = None


class EnrichmentResult(BaseModel):
    person: Person
    candidates: list[EmailCandidate] = Field(default_factory=list)
    providers_attempted: list[str] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    completed_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def best(self) -> EmailCandidate | None:
        if not self.candidates:
            return None
        return min(self.candidates, key=lambda c: c.confidence.rank)

    @property
    def is_strong(self) -> bool:
        """HIGH confidence AND verified — earns short-circuit through the cascade."""
        return any(
            c.confidence == Confidence.HIGH and c.verified for c in self.candidates
        )

    @property
    def has_any(self) -> bool:
        return any(c.email for c in self.candidates)

    @property
    def has_medium_or_better(self) -> bool:
        return any(
            c.email and c.confidence.rank <= Confidence.MEDIUM.rank
            for c in self.candidates
        )
