"""Exa neural-search provider (stub).

Exa can search the open web for a person's email by querying things like:
  '"<first> <last>" email "<school>"' or '"<first> <last>" "@<domain>"'
and extracting matches from the snippets/pages returned.

To enable: implement enrich(), set EXA_API_KEY, register in providers/__init__.py.
"""
from __future__ import annotations

from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import EmailCandidate, Person


class ExaProvider(EnrichmentProvider):
    name = "exa"
    cost_per_call_usd = 0.005

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        raise NotImplementedError("Exa provider not yet implemented")
