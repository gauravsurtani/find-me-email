from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from find_me_email.schemas import EmailCandidate, Person


class EnrichmentProvider(ABC):
    """Implement this to plug a new data source into the cascade.

    Each provider is responsible for ONE data source. The orchestrator handles
    cascade ordering, budget enforcement, retries, and result merging.
    """

    name: str = "unnamed"
    cost_per_call_usd: float = 0.0

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    def can_handle(self, person: Person) -> bool:
        """Return False to skip this person (e.g., college guesser needs a school)."""
        return True

    @abstractmethod
    async def enrich(self, person: Person) -> list[EmailCandidate]:
        """Return zero or more email candidates for this person."""

    async def enrich_batch(self, people: list[Person]) -> dict[str, list[EmailCandidate]]:
        """Override for providers that batch (e.g., Apify actors that take a list).

        Returns: {row_id: [candidates]}
        Default implementation calls enrich() per person.
        """
        out: dict[str, list[EmailCandidate]] = {}
        for p in people:
            if self.can_handle(p):
                out[p.row_id] = await self.enrich(p)
            else:
                out[p.row_id] = []
        return out
