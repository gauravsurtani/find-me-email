"""Broad Google SERP provider via Apify (no `site:` restriction).

Complement to `apify_school_serp` (school-domain-locked) and `exa` (neural).
Hits Google's regular index for personal sites, conference pages, GitHub
READMEs, and paper PDFs.
"""
from __future__ import annotations

import asyncio
import re
import string
from typing import Any

import httpx

from find_me_email.apify_client import ApifyClient
from find_me_email.providers._emailmatch import (
    EMAIL_RE,
    PERSONAL_PROVIDER_DOMAINS,
    is_usable_local_part,
    loose_match_query,
    match_flags,
    merge_candidate,
    normalize_email,
    person_tokens,
)
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import Confidence, EmailCandidate, Person

DEFAULT_ACTOR = "apify/google-search-scraper"


class ApifyBroadSerpProvider(EnrichmentProvider):
    name = "apify_broad_serp"
    cost_per_call_usd = 0.005

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.actor_id: str = self.config.get("actor_id", DEFAULT_ACTOR)
        self.timeout_s: int = int(self.config.get("timeout_s", 1200))
        self.results_per_query: int = int(self.config.get("results_per_query", 10))
        self.country_code: str = self.config.get("country_code", "us")
        self.language_code: str = self.config.get("language_code", "en")
        self.fetch_top_pages: int = int(self.config.get("fetch_top_pages", 3))
        self.fetch_timeout_s: float = float(self.config.get("fetch_timeout_s", 8.0))
        self.query_templates: list[str] = self.config.get(
            "query_templates",
            [
                '"{name}" "{school}" email',
                '"{name}" CV resume contact',
            ],
        )

    def can_handle(self, person: Person) -> bool:
        return bool(person.name)

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        return (await self.enrich_batch([person])).get(person.row_id, [])

    async def enrich_batch(
        self, people: list[Person]
    ) -> dict[str, list[EmailCandidate]]:
        out: dict[str, list[EmailCandidate]] = {p.row_id: [] for p in people}
        targets = [p for p in people if self.can_handle(p)]
        if not targets:
            return out

        query_to_row: dict[str, str] = {}
        queries: list[str] = []
        for person in targets:
            for q in self._build_queries(person):
                if q in query_to_row:
                    continue
                query_to_row[q] = person.row_id
                queries.append(q)
        if not queries:
            return out

        payload = {
            "queries": "\n".join(queries),
            "resultsPerPage": self.results_per_query,
            "maxPagesPerQuery": 1,
            "countryCode": self.country_code,
            "languageCode": self.language_code,
            "saveHtml": False,
            "mobileResults": False,
        }

        async with ApifyClient() as ac:
            items = await ac.run_actor_sync(
                self.actor_id, payload, wait_secs=self.timeout_s
            )

        per_person: dict[str, dict[str, EmailCandidate]] = {p.row_id: {} for p in people}
        followups: list[tuple[str, str]] = []
        person_by_row = {p.row_id: p for p in people}
        tokens_by_row = {p.row_id: person_tokens(p) for p in people}
        domain_by_row = {p.row_id: (p.school_domain or "").lower() for p in people}

        for item in items:
            term = (item.get("searchQuery") or {}).get("term") or ""
            row_id = query_to_row.get(term) or loose_match_query(term, query_to_row)
            if row_id is None:
                continue
            tokens = tokens_by_row[row_id]
            target_domain = domain_by_row[row_id]

            page_fetch_count = 0
            for result in item.get("organicResults") or []:
                url = result.get("url") or ""
                blob = " ".join(
                    [result.get("title") or "", result.get("description") or ""]
                )

                for raw in EMAIL_RE.findall(blob):
                    email = normalize_email(raw)
                    if not is_usable_local_part(email):
                        continue
                    self._record(
                        per_person[row_id], email, tokens, target_domain, url, term, "snippet"
                    )

                if (
                    page_fetch_count < self.fetch_top_pages
                    and not EMAIL_RE.search(blob)
                    and url
                ):
                    followups.append((row_id, url))
                    page_fetch_count += 1

        if followups:
            await self._fetch_followups(
                followups, tokens_by_row, domain_by_row, per_person
            )

        per_person_cost = self.cost_per_call_usd
        for row_id, by_email in per_person.items():
            for cand in by_email.values():
                cand.cost_usd = round(per_person_cost / max(len(by_email), 1), 6)
                out[row_id].append(cand)
        return out

    # ------------------------------------------------------ helpers

    def _build_queries(self, person: Person) -> list[str]:
        ctx = {
            "name": (person.name or "").strip(),
            "school": (person.school or "").strip(),
            "company": (person.company or "").strip(),
            "domain": (person.school_domain or "").strip(),
        }
        out: list[str] = []
        for tmpl in self.query_templates:
            try:
                # Skip templates whose required slots are empty so we don't
                # produce nonsense like '"Jane Doe" ""  email'.
                needed = {f for _, f, _, _ in string.Formatter().parse(tmpl) if f}
                if any(not ctx.get(f) for f in needed):
                    continue
                q = re.sub(r"\s{2,}", " ", tmpl.format(**ctx).strip())
                if q and q not in out:
                    out.append(q)
            except (KeyError, IndexError):
                continue
        return out

    def _record(
        self,
        bag: dict[str, EmailCandidate],
        email: str,
        tokens: set[str],
        target_domain: str,
        source_url: str,
        query: str,
        origin: str,
    ) -> None:
        confidence, base = self._score(email, tokens, target_domain)
        merge_candidate(
            bag,
            email=email,
            confidence=confidence,
            notes=f"{base} ({origin})",
            source_provider=self.name,
            raw={"source_url": source_url, "query": query, "via": origin},
        )

    @staticmethod
    def _score(
        email: str, tokens: set[str], target_domain: str
    ) -> tuple[Confidence, str]:
        token_match, domain_match = match_flags(email, tokens, target_domain)
        dom = email.partition("@")[2]
        if token_match and domain_match:
            return Confidence.MEDIUM, "broad_serp: name+school both match"
        if token_match and dom in PERSONAL_PROVIDER_DOMAINS:
            return (
                Confidence.LOW,
                "broad_serp: name matches local-part on personal-email provider",
            )
        if token_match:
            return Confidence.LOW, "broad_serp: local-part matches name"
        if domain_match:
            return Confidence.LOW, "broad_serp: domain matches school but local-part doesn't"
        return Confidence.SPECULATIVE, "broad_serp: weak match (no name/domain overlap)"

    async def _fetch_followups(
        self,
        followups: list[tuple[str, str]],
        tokens_by_row: dict[str, set[str]],
        domain_by_row: dict[str, str],
        per_person: dict[str, dict[str, EmailCandidate]],
    ) -> None:
        sem = asyncio.Semaphore(8)
        async with httpx.AsyncClient(
            timeout=self.fetch_timeout_s,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; find-me-email/1.0; "
                    "+https://github.com/gauravsurtani/find-me-email)"
                )
            },
        ) as client:

            async def _one(row_id: str, url: str) -> None:
                async with sem:
                    try:
                        r = await client.get(url)
                    except httpx.HTTPError:
                        return
                    if r.status_code >= 400:
                        return
                    tokens = tokens_by_row[row_id]
                    target_domain = domain_by_row[row_id]
                    for raw in EMAIL_RE.findall(r.text):
                        email = normalize_email(raw)
                        if not is_usable_local_part(email):
                            continue
                        self._record(
                            per_person[row_id],
                            email,
                            tokens,
                            target_domain,
                            url,
                            "(page fetch)",
                            "page",
                        )

            await asyncio.gather(
                *(_one(rid, url) for rid, url in followups),
                return_exceptions=True,
            )
