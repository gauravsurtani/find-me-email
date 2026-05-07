"""Exa neural-search provider.

Strategy: query Exa for the person's name + school. Exa returns a list of
URLs with text content. We extract email-shaped strings from the results,
then filter for ones that plausibly belong to this person (name tokens in
local part, or domain matches the person's school).

Endpoint: POST https://api.exa.ai/search
Auth:     x-api-key header
Docs:     https://docs.exa.ai/

The provider treats Exa as a "find emails ON THE OPEN WEB" complement to
the LinkedIn-keyed B2B providers — it surfaces emails on personal websites,
conference pages, lab pages, GitHub READMEs, etc.
"""
from __future__ import annotations

from typing import Any

import httpx

from find_me_email.providers._emailmatch import (
    EMAIL_RE,
    match_flags,
    normalize_email,
    person_tokens,
)
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import Confidence, EmailCandidate, Person
from find_me_email.settings import settings

EXA_API = "https://api.exa.ai/search"


class ExaProvider(EnrichmentProvider):
    name = "exa"
    cost_per_call_usd = 0.005  # ~$5/1K searches on Exa's standard tier

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.api_key: str = self.config.get("api_key") or settings.exa_api_key
        self.num_results: int = int(self.config.get("num_results", 5))
        self.use_contents: bool = bool(self.config.get("use_contents", True))
        self.timeout_s: float = float(self.config.get("timeout_s", 30.0))

    def can_handle(self, person: Person) -> bool:
        return bool(self.api_key) and bool(person.name)

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        return (await self.enrich_batch([person])).get(person.row_id, [])

    async def enrich_batch(self, people: list[Person]) -> dict[str, list[EmailCandidate]]:
        out: dict[str, list[EmailCandidate]] = {p.row_id: [] for p in people}
        if not self.api_key:
            return out
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            for p in people:
                if not self.can_handle(p):
                    continue
                try:
                    out[p.row_id] = await self._enrich_one(client, p)
                except Exception as e:
                    out[p.row_id].append(self._error_candidate(str(e)))
        return out

    async def _enrich_one(self, client: httpx.AsyncClient, person: Person) -> list[EmailCandidate]:
        query = self._build_query(person)
        payload: dict[str, Any] = {
            "query": query,
            "numResults": self.num_results,
            "type": "auto",
        }
        if self.use_contents:
            payload["contents"] = {"text": {"maxCharacters": 1500}}
        r = await client.post(
            EXA_API,
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json().get("results", [])
        return self._extract_candidates(data, person, query)

    def _build_query(self, person: Person) -> str:
        parts = [f'"{person.name}"']
        if person.school:
            parts.append(f'"{person.school}"')
        if person.school_domain:
            parts.append(f'"@{person.school_domain}"')
        parts.append("email")
        return " ".join(parts)

    def _extract_candidates(
        self, results: list[dict[str, Any]], person: Person, query: str
    ) -> list[EmailCandidate]:
        out: list[EmailCandidate] = []
        seen: set[str] = set()
        tokens = person_tokens(person)
        target_domain = (person.school_domain or "").lower()

        for r in results:
            url = r.get("url", "")
            text_blobs = [
                r.get("text") or "",
                r.get("title") or "",
                r.get("highlights") and " ".join(r["highlights"]) or "",
            ]
            for blob in text_blobs:
                for match in EMAIL_RE.findall(blob):
                    email = normalize_email(match)
                    if email in seen:
                        continue
                    seen.add(email)
                    confidence, notes = self._score(email, tokens, target_domain)
                    out.append(
                        EmailCandidate(
                            email=email,
                            confidence=confidence,
                            source_provider=self.name,
                            verified=False,
                            notes=notes,
                            cost_usd=self.cost_per_call_usd / max(len(results), 1),
                            raw={"source_url": url, "query": query},
                        )
                    )
        return out

    @staticmethod
    def _score(email: str, tokens: set[str], target_domain: str) -> tuple[Confidence, str]:
        token_match, domain_match = match_flags(email, tokens, target_domain)
        if token_match and domain_match:
            return Confidence.MEDIUM, "Exa: name+school both present in result"
        if domain_match:
            return Confidence.LOW, "Exa: domain matches target school"
        if token_match:
            return Confidence.LOW, "Exa: local-part contains person's name"
        return Confidence.SPECULATIVE, "Exa: email found in search result, weak match to person"

    @staticmethod
    def _error_candidate(msg: str) -> EmailCandidate:
        return EmailCandidate(
            email="",
            confidence=Confidence.SPECULATIVE,
            source_provider="exa",
            verified=False,
            notes=f"Exa error: {msg}",
        )
