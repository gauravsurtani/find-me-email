"""Hunter.io provider (stub).

Two endpoints we'd use:
  - email-finder: GET /v2/email-finder?domain=...&first_name=...&last_name=...
  - email-verifier: GET /v2/email-verifier?email=...

Useful both as a finder AND as a verifier for our pattern guesses.
"""
from __future__ import annotations

from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import EmailCandidate, Person


class HunterProvider(EnrichmentProvider):
    name = "hunter"
    cost_per_call_usd = 0.0  # free tier; raise if you go paid

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        raise NotImplementedError("Hunter provider not yet implemented")
