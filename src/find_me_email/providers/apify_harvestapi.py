from __future__ import annotations

from typing import Any

from find_me_email.apify_client import ApifyClient
from find_me_email.providers.apify_b2b_leads import ApifyB2BLeadsProvider
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import Confidence, EmailCandidate, Person


class ApifyHarvestAPIProvider(EnrichmentProvider):
    """Apify actor: harvestapi/linkedin-profile-scraper. Higher-quality fallback with SMTP-validated email."""

    name = "apify_harvestapi"
    cost_per_call_usd = 0.008  # ~$8 / 1K profiles with email mode

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.actor_id: str = self.config.get("actor_id", "harvestapi/linkedin-profile-scraper")
        self.timeout_s: int = int(self.config.get("timeout_s", 600))
        self.mode: str = self.config.get("mode", "full_with_email")

    def can_handle(self, person: Person) -> bool:
        return person.linkedin_url is not None

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        return (await self.enrich_batch([person])).get(person.row_id, [])

    async def enrich_batch(self, people: list[Person]) -> dict[str, list[EmailCandidate]]:
        targets = [p for p in people if self.can_handle(p)]
        if not targets:
            return {p.row_id: [] for p in people}

        url_to_row: dict[str, str] = {str(p.linkedin_url): p.row_id for p in targets}
        payload = {
            "profileScraperMode": self.mode,
            "queries": list(url_to_row.keys()),
        }

        async with ApifyClient() as ac:
            items = await ac.run_actor_sync(self.actor_id, payload, wait_secs=self.timeout_s)

        helper = ApifyB2BLeadsProvider()
        out: dict[str, list[EmailCandidate]] = {p.row_id: [] for p in people}
        for item in items:
            row_id = helper._match_row(item, url_to_row)  # noqa: SLF001
            if not row_id:
                continue
            for cand in helper._extract_emails(item):  # noqa: SLF001
                cand.source_provider = self.name
                cand.confidence = Confidence.HIGH if item.get("emailVerified") else Confidence.MEDIUM
                cand.verified = bool(item.get("emailVerified"))
                cand.verification_method = "smtp" if cand.verified else None
                cand.cost_usd = self.cost_per_call_usd
                cand.notes = "harvestapi DB match" + (" (SMTP-verified)" if cand.verified else "")
                out[row_id].append(cand)
        return out
