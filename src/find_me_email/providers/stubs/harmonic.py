"""Harmonic provider (stub).

Harmonic MCP server (already connected) exposes get_people / get_person_connections
which include emails when present in the network. This provider would call those
tools through the MCP layer rather than HTTP — wire it once we move into the
SDK / orchestrator that has MCP access.
"""
from __future__ import annotations

from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import EmailCandidate, Person


class HarmonicProvider(EnrichmentProvider):
    name = "harmonic"
    cost_per_call_usd = 0.0  # included in existing Harmonic subscription

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        raise NotImplementedError("Harmonic provider not yet implemented (needs MCP wiring)")
