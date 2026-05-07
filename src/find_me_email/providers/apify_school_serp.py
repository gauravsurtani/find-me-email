"""School-domain Google SERP provider (via Apify google-search-scraper).

Strategy:
1. For each person with a known school, build queries restricting Google to
   the school's `.edu` domain:
     "<name>" "@<domain>" site:<domain>
   This forces results to come from the school's own subdomains — exactly
   where directories, department pages, and lab sites live.
2. Submit ALL queries in a single Apify actor run (one billable run, not 57).
3. Match results back to people via the echoed query string.
4. Extract emails from SERP snippets (title + description). Snippets often
   already contain "name <jdoe@school.edu>" on directory pages.
5. For top-N results on the target domain with no email in the snippet,
   fetch the page directly with httpx and regex-extract.
6. Score each candidate by name-token + domain match (same scheme as Exa).

Why this beats Exa for `.edu` lookups:
- Exa's neural index is optimized for prose-rich pages; small directory
  entries with short names + bare emails often rank low or are missed.
- `site:` queries force domain-locked results; we never get the wrong
  person at a different institution (a recurring Exa failure mode).

Apify actor: `apify/google-search-scraper` (configurable).
Approx cost: ~$1.50 / 1000 queries with default proxy. For ~60 people *
~2 domains = ~120 queries, run cost is roughly $0.20.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from find_me_email.apify_client import ApifyClient
from find_me_email.college_domains import domains_for, resolve_domain
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import Confidence, EmailCandidate, Person

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DEFAULT_ACTOR = "apify/google-search-scraper"


class ApifySchoolSerpProvider(EnrichmentProvider):
    name = "apify_school_serp"
    # Rough — actor bills by Compute Units; ~$1.50/1000 queries on standard proxy.
    # We charge per *person* (not per query) to keep budget math simple.
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

        # Build one query per (person, domain). Track which person each query
        # belongs to so we can attribute results when they come back.
        query_to_row: dict[str, str] = {}
        query_to_domain: dict[str, str] = {}
        queries: list[str] = []
        for person in targets:
            for domain in self._domains_for(person)[: self.max_domains_per_person]:
                q = self._build_query(person, domain)
                # Identical queries (e.g., two people with the same name on
                # the same domain — unlikely) would collide, but the cost of
                # collision is just cross-attribution; skip duplicates.
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

        # Per-person aggregator. Use a dict-of-dict keyed by email to merge
        # multiple sightings of the same address (snippet + page-fetch).
        per_person: dict[str, dict[str, EmailCandidate]] = {p.row_id: {} for p in people}
        # Collect URLs that warrant a page-fetch follow-up.
        followups: list[tuple[str, str, str]] = []  # (row_id, domain, url)

        person_by_row = {p.row_id: p for p in people}

        for item in items:
            term = (item.get("searchQuery") or {}).get("term") or ""
            row_id = query_to_row.get(term)
            if row_id is None:
                # Try a loose match (Apify sometimes normalizes the query)
                row_id = self._loose_match(term, query_to_row)
            if row_id is None:
                continue
            domain = query_to_domain.get(term, "")
            person = person_by_row.get(row_id)
            if person is None:
                continue

            organic = item.get("organicResults") or []
            person_tokens = self._person_tokens(person)
            page_fetch_count = 0

            for result in organic:
                url = result.get("url") or ""
                title = result.get("title") or ""
                description = result.get("description") or ""
                blob = " ".join([title, description])

                # Pull any emails directly from the snippet.
                for match in EMAIL_RE.findall(blob):
                    email = self._normalize_email(match)
                    self._merge(
                        per_person[row_id],
                        email,
                        person_tokens,
                        domain,
                        source_url=url,
                        query=term,
                        notes_prefix="snippet",
                    )

                # Queue page fetch for promising-looking results without an email.
                if (
                    page_fetch_count < self.fetch_top_pages
                    and not EMAIL_RE.search(blob)
                    and domain
                    and self._url_on_domain(url, domain)
                ):
                    followups.append((row_id, domain, url))
                    page_fetch_count += 1

        # Page-fetch the queued URLs in parallel (free, just a regular HTTP GET).
        if followups:
            await self._fetch_followups(followups, person_by_row, per_person)

        # Materialize candidates with cost evenly attributed across people.
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
        # Dedup while preserving order
        seen, out = set(), []
        for d in base:
            d = d.lower().strip()
            if d and d not in seen:
                seen.add(d)
                out.append(d)
        return out

    @staticmethod
    def _build_query(person: Person, domain: str) -> str:
        name = (person.name or "").strip()
        return f'"{name}" "@{domain}" site:{domain}'

    @staticmethod
    def _normalize_email(raw: str) -> str:
        return raw.lower().strip(".,;:()<>[]{}\"' ")

    @staticmethod
    def _url_on_domain(url: str, domain: str) -> bool:
        if not url or not domain:
            return False
        u = url.lower()
        # We accept the domain itself or any subdomain of its registrable parent.
        # E.g., target=cs.stanford.edu → also accept stanford.edu and *.stanford.edu.
        parent = ".".join(domain.split(".")[-2:]) if domain.count(".") >= 2 else domain
        return f"//{domain}" in u or f".{parent}" in u or f"//{parent}" in u

    @staticmethod
    def _loose_match(term: str, query_to_row: dict[str, str]) -> str | None:
        # Apify sometimes returns the query with collapsed whitespace; try a
        # whitespace-insensitive lookup.
        norm = re.sub(r"\s+", " ", term.strip().lower())
        for q, rid in query_to_row.items():
            if re.sub(r"\s+", " ", q.strip().lower()) == norm:
                return rid
        return None

    @staticmethod
    def _person_tokens(person: Person) -> set[str]:
        tokens: set[str] = set()
        for s in (person.name, person.first_name, person.last_name):
            if not s:
                continue
            for t in re.split(r"[^a-z]+", s.lower()):
                if len(t) >= 3:
                    tokens.add(t)
        return tokens

    @staticmethod
    def _score(
        email: str, person_tokens: set[str], target_domain: str
    ) -> tuple[Confidence, str]:
        local, _, dom = email.partition("@")
        local_lower = local.lower()
        token_match = (
            any(tok in local_lower for tok in person_tokens) if person_tokens else False
        )
        domain_match = bool(target_domain) and dom.endswith(target_domain.lstrip("."))
        if token_match and domain_match:
            return Confidence.MEDIUM, "school_serp: name+school both match"
        if domain_match:
            return Confidence.LOW, "school_serp: domain matches but local-part doesn't include name"
        if token_match:
            return Confidence.LOW, "school_serp: local-part matches name but wrong domain"
        return Confidence.SPECULATIVE, "school_serp: weak match (different name+domain)"

    def _merge(
        self,
        bag: dict[str, EmailCandidate],
        email: str,
        person_tokens: set[str],
        domain: str,
        source_url: str,
        query: str,
        notes_prefix: str,
    ) -> None:
        if not email or "@" not in email:
            return
        confidence, base_note = self._score(email, person_tokens, domain)
        existing = bag.get(email)
        note = f"{base_note} ({notes_prefix})"
        if existing is None:
            bag[email] = EmailCandidate(
                email=email,
                confidence=confidence,
                source_provider=self.name,
                verified=False,
                notes=note,
                raw={"source_url": source_url, "query": query, "via": notes_prefix},
            )
            return
        # Promote confidence if new sighting is stronger.
        order = {
            Confidence.HIGH: 0,
            Confidence.MEDIUM: 1,
            Confidence.LOW: 2,
            Confidence.SPECULATIVE: 3,
        }
        if order[confidence] < order[existing.confidence]:
            existing.confidence = confidence
            existing.notes = note

    async def _fetch_followups(
        self,
        followups: list[tuple[str, str, str]],
        person_by_row: dict[str, Person],
        per_person: dict[str, dict[str, EmailCandidate]],
    ) -> None:
        """Fetch page contents (parallel, bounded concurrency) and extract emails."""
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
                        if r.status_code >= 400:
                            return
                        text = r.text
                    except Exception:
                        return
                    person = person_by_row.get(row_id)
                    if person is None:
                        return
                    person_tokens = self._person_tokens(person)
                    for match in EMAIL_RE.findall(text):
                        email = self._normalize_email(match)
                        # Skip obvious junk: webmaster@, info@, no-reply@, etc.
                        local = email.split("@", 1)[0]
                        if local in {
                            "webmaster",
                            "info",
                            "contact",
                            "support",
                            "noreply",
                            "no-reply",
                            "admin",
                            "help",
                            "hello",
                        }:
                            continue
                        self._merge(
                            per_person[row_id],
                            email,
                            person_tokens,
                            domain,
                            source_url=url,
                            query="(page fetch)",
                            notes_prefix="page",
                        )

            await asyncio.gather(
                *(_one(rid, dom, url) for rid, dom, url in followups),
                return_exceptions=True,
            )
