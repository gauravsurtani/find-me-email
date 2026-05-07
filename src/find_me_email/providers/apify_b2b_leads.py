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
        payload = {
            "profileUrls": list(url_to_row.keys()),
            "enableEmailEnrichment": True,
        }

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
        # Build identifier index: every URL maps to the row by public identifier
        # (last path segment) and by url-with-/-without "www." normalized.
        def _ident(u: str) -> str:
            u = u.strip().rstrip("/").lower()
            if "/in/" in u:
                u = u.split("/in/", 1)[1].split("/", 1)[0].split("?", 1)[0]
            return u

        ident_to_row = {_ident(u): rid for u, rid in url_to_row.items()}
        # Try the obvious URL fields, then fall back to the actor's own publicIdentifier.
        for k in ("publicIdentifier", "public_identifier", "profileUrl", "linkedinUrl",
                  "url", "input"):
            v = item.get(k)
            if isinstance(v, str):
                ident = _ident(v)
                if ident in ident_to_row:
                    return ident_to_row[ident]
        # Last resort: scan all string fields.
        for v in item.values():
            if isinstance(v, str):
                ident = _ident(v)
                if ident in ident_to_row:
                    return ident_to_row[ident]
        return None

    def _extract_emails(self, item: dict[str, Any]) -> list[EmailCandidate]:
        # email -> per-email metadata (deliverable/status/qualityScore) when available
        found: dict[str, dict[str, Any]] = {}

        def _add(addr: str | None, meta: dict[str, Any] | None = None):
            if not isinstance(addr, str) or "@" not in addr:
                return
            key = addr.strip().lower()
            if key not in found:
                found[key] = meta or {}

        for k in ("email", "emailAddress", "workEmail", "personalEmail", "best_email"):
            _add(item.get(k))
        for k in ("emails", "contactEmails"):
            v = item.get(k)
            if isinstance(v, list):
                for e in v:
                    if isinstance(e, str):
                        _add(e)
                    elif isinstance(e, dict):
                        _add(e.get("email") or e.get("emailAddress") or e.get("address"), e)
        cands = []
        for addr, meta in found.items():
            verified = bool(meta.get("deliverable") or meta.get("status") == "valid")
            cands.append(
                EmailCandidate(
                    email=addr,
                    confidence=Confidence.HIGH if verified else Confidence.MEDIUM,
                    source_provider=self.name,
                    verified=verified,
                    notes="Direct DB match from b2b_leads actor"
                    + (f" (status={meta.get('status')}, score={meta.get('qualityScore')})" if meta else ""),
                    raw=item,
                )
            )
        return cands
