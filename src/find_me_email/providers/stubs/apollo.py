"""Apollo.io People Enrichment provider (stub).

POST https://api.apollo.io/v1/people/match with name + linkedin_url.
Returns work email when found. Generous free tier.
"""
from __future__ import annotations

from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import EmailCandidate, Person


class ApolloProvider(EnrichmentProvider):
    name = "apollo"
    cost_per_call_usd = 0.0  # free tier; raise if you go paid

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        raise NotImplementedError("Apollo provider not yet implemented")
