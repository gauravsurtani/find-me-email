from __future__ import annotations

import re
from typing import Any

from find_me_email.apify_client import ApifyClient
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import Confidence, EmailCandidate, Person

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class ApifyB2BLeadsProvider(EnrichmentProvider):
    """Apify actor: b2b_leads/linkedin-profile-scraper.

    Pay-per-result LinkedIn-URL → verified-email finder. Cheapest first pass.
    """

    name = "apify_b2b_leads"
    cost_per_call_usd = 0.0015  # ~$1.5 / 1K profiles, only billed on result

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.actor_id: str = self.config.get("actor_id", "b2b_leads/linkedin-profile-scraper")
        self.timeout_s: int = int(self.config.get("timeout_s", 600))

    def can_handle(self, person: Person) -> bool:
        return person.linkedin_url is not None

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        return (await self.enrich_batch([person])).get(person.row_id, [])

    async def enrich_batch(self, people: list[Person]) -> dict[str, list[EmailCandidate]]:
        targets = [p for p in people if self.can_handle(p)]
        if not targets:
            return {p.row_id: [] for p in people}

        url_to_row: dict[str, str] = {str(p.linkedin_url): p.row_id for p in targets}
        payload = {"profileUrls": list(url_to_row.keys())}

        async with ApifyClient() as ac:
            items = await ac.run_actor_sync(self.actor_id, payload, wait_secs=self.timeout_s)

        out: dict[str, list[EmailCandidate]] = {p.row_id: [] for p in people}
        for item in items:
            row_id = self._match_row(item, url_to_row)
            if not row_id:
                continue
            for cand in self._extract_emails(item):
                cand.source_provider = self.name
                cand.cost_usd = self.cost_per_call_usd
                out[row_id].append(cand)
        return out

    def _match_row(self, item: dict[str, Any], url_to_row: dict[str, str]) -> str | None:
        for k in ("profileUrl", "linkedinUrl", "url", "input"):
            v = item.get(k)
            if isinstance(v, str) and v in url_to_row:
                return url_to_row[v]
        for url, rid in url_to_row.items():
            if any(url in str(v) for v in item.values() if isinstance(v, str)):
                return rid
        return None

    def _extract_emails(self, item: dict[str, Any]) -> list[EmailCandidate]:
        emails: set[str] = set()
        for k in ("email", "emailAddress", "workEmail", "personalEmail"):
            v = item.get(k)
            if isinstance(v, str) and "@" in v:
                emails.add(v.strip().lower())
        for k in ("emails", "contactEmails"):
            v = item.get(k)
            if isinstance(v, list):
                for e in v:
                    if isinstance(e, str) and "@" in e:
                        emails.add(e.strip().lower())
        return [
            EmailCandidate(
                email=e,
                confidence=Confidence.MEDIUM,
                source_provider=self.name,
                verified=False,
                notes="Direct DB match from b2b_leads actor",
                raw=item,
            )
            for e in emails
        ]
