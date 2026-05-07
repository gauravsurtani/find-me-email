"""School-domain Google SERP provider via Apify google-search-scraper.

Restricts queries to the school's own .edu domain (`site:<domain>`) so
results come from directories, lab pages, and profile sites — avoiding
wrong-person hits at other institutions.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from find_me_email.apify_client import ApifyClient
from find_me_email.college_domains import domains_for
from find_me_email.providers._emailmatch import (
    EMAIL_RE,
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


class ApifySchoolSerpProvider(EnrichmentProvider):
    name = "apify_school_serp"
    cost_per_call_usd = 0.005

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.actor_id: str = self.config.get("actor_id", DEFAULT_ACTOR)
        self.timeout_s: int = int(self.config.get("timeout_s", 1200))
        self.results_per_query: int = int(self.config.get("results_per_query", 10))
        self.max_domains_per_person: int = int(self.config.get("max_domains_per_person", 2))
        self.country_code: str = self.config.get("country_code", "us")
        self.language_code: str = self.config.get("language_code", "en")
        # If a SERP result is on the target domain but its snippet has no email,
        # follow up by fetching the page (free, via httpx) and regex-extracting.
        self.fetch_top_pages: int = int(self.config.get("fetch_top_pages", 3))
        self.fetch_timeout_s: float = float(self.config.get("fetch_timeout_s", 8.0))

    def can_handle(self, person: Person) -> bool:
        if not person.name:
            return False
        return bool(self._domains_for(person))

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        return (await self.enrich_batch([person])).get(person.row_id, [])

    async def enrich_batch(self, people: list[Person]) -> dict[str, list[EmailCandidate]]:
        out: dict[str, list[EmailCandidate]] = {p.row_id: [] for p in people}
        targets = [p for p in people if self.can_handle(p)]
        if not targets:
            return out

        query_to_row: dict[str, str] = {}
        query_to_domain: dict[str, str] = {}
        queries: list[str] = []
        for person in targets:
            for domain in self._domains_for(person)[: self.max_domains_per_person]:
                q = self._build_query(person, domain)
                if q in query_to_row:
                    continue
                query_to_row[q] = person.row_id
                query_to_domain[q] = domain
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
            items = await ac.run_actor_sync(self.actor_id, payload, wait_secs=self.timeout_s)

        per_person: dict[str, dict[str, EmailCandidate]] = {p.row_id: {} for p in people}
        followups: list[tuple[str, str, str]] = []  # (row_id, domain, url)
        person_by_row = {p.row_id: p for p in people}
        tokens_by_row = {p.row_id: person_tokens(p) for p in people}

        for item in items:
            term = (item.get("searchQuery") or {}).get("term") or ""
            row_id = query_to_row.get(term) or loose_match_query(term, query_to_row)
            if row_id is None:
                continue
            domain = query_to_domain.get(term, "")
            tokens = tokens_by_row.get(row_id, set())
            page_fetch_count = 0

            for result in item.get("organicResults") or []:
                url = result.get("url") or ""
                blob = " ".join(
                    [result.get("title") or "", result.get("description") or ""]
                )

                for raw in EMAIL_RE.findall(blob):
                    email = normalize_email(raw)
                    self._record(
                        per_person[row_id], email, tokens, domain, url, term, "snippet"
                    )

                if (
                    page_fetch_count < self.fetch_top_pages
                    and not EMAIL_RE.search(blob)
                    and domain
                    and self._url_on_domain(url, domain)
                ):
                    followups.append((row_id, domain, url))
                    page_fetch_count += 1

        if followups:
            await self._fetch_followups(followups, tokens_by_row, per_person)

        per_person_cost = self.cost_per_call_usd
        for row_id, by_email in per_person.items():
            for cand in by_email.values():
                cand.cost_usd = round(per_person_cost / max(len(by_email), 1), 6)
                out[row_id].append(cand)
        return out

    # --------------------------------------------------------------------- helpers

    def _domains_for(self, person: Person) -> list[str]:
        if person.school_domain:
            base = [person.school_domain]
        elif person.school:
            base = domains_for(person.school)
        else:
            base = []
        seen, out = set(), []
        for d in base:
            d = d.lower().strip()
            if d and d not in seen:
                seen.add(d)
                out.append(d)
        return out

    @staticmethod
    def _build_query(person: Person, domain: str) -> str:
        return f'"{(person.name or "").strip()}" "@{domain}" site:{domain}'

    @staticmethod
    def _url_on_domain(url: str, domain: str) -> bool:
        # Accept the domain or any subdomain of its registrable parent.
        # E.g., target=cs.stanford.edu accepts stanford.edu and *.stanford.edu.
        if not url or not domain:
            return False
        u = url.lower()
        parent = ".".join(domain.split(".")[-2:]) if domain.count(".") >= 2 else domain
        return f"//{domain}" in u or f".{parent}" in u or f"//{parent}" in u

    def _record(
        self,
        bag: dict[str, EmailCandidate],
        email: str,
        tokens: set[str],
        domain: str,
        source_url: str,
        query: str,
        origin: str,
    ) -> None:
        confidence, base = self._score(email, tokens, domain)
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
        if token_match and domain_match:
            return Confidence.MEDIUM, "school_serp: name+school both match"
        if domain_match:
            return Confidence.LOW, "school_serp: domain matches but local-part doesn't include name"
        if token_match:
            return Confidence.LOW, "school_serp: local-part matches name but wrong domain"
        return Confidence.SPECULATIVE, "school_serp: weak match (different name+domain)"

    async def _fetch_followups(
        self,
        followups: list[tuple[str, str, str]],
        tokens_by_row: dict[str, set[str]],
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

            async def _one(row_id: str, domain: str, url: str) -> None:
                async with sem:
                    try:
                        r = await client.get(url)
                    except httpx.HTTPError:
                        return
                    if r.status_code >= 400:
                        return
                    tokens = tokens_by_row.get(row_id, set())
                    for raw in EMAIL_RE.findall(r.text):
                        email = normalize_email(raw)
                        if not is_usable_local_part(email):
                            continue
                        self._record(
                            per_person[row_id],
                            email,
                            tokens,
                            domain,
                            url,
                            "(page fetch)",
                            "page",
                        )

            await asyncio.gather(
                *(_one(rid, dom, url) for rid, dom, url in followups),
                return_exceptions=True,
            )
