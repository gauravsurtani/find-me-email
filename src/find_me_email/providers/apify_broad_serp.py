"""Broad Google SERP provider via Apify (no `site:` restriction).

Complement to `apify_school_serp` (which is school-domain-locked) and `exa`
(neural search). This one hits Google's regular index with broader queries
to surface emails on:
  - Personal websites + portfolio pages
  - GitHub README files
  - Conference attendee/speaker pages
  - News / press / interviews
  - arXiv + paper preprint PDFs (which often have author emails)

Why not just rely on Exa? Exa's neural ranking sometimes misses sparse
contact-page hits and over-weights prose-rich content. Direct Google SERP
catches "tail" pages Exa skips, especially for students whose presence is
limited to a single CV page or conference appearance.

Same actor + scoring logic as `apify_school_serp`; the difference is purely
in the query template and how we score domain matches.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from find_me_email.apify_client import ApifyClient
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import Confidence, EmailCandidate, Person

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DEFAULT_ACTOR = "apify/google-search-scraper"

# Public-noise local parts that show up in scraped HTML but rarely belong
# to the target person.
ROLE_LOCAL_PARTS = {
    "info",
    "contact",
    "support",
    "admin",
    "help",
    "hello",
    "webmaster",
    "noreply",
    "no-reply",
    "press",
    "marketing",
    "sales",
    "hr",
    "jobs",
    "careers",
    "team",
    "office",
    "general",
}


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
        # Override: provide a list of query templates. Each template is
        # `.format(name=..., school=..., domain=...)`-substituted per person.
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
        followups: list[tuple[str, str]] = []  # (row_id, url)
        person_by_row = {p.row_id: p for p in people}

        for item in items:
            term = (item.get("searchQuery") or {}).get("term") or ""
            row_id = query_to_row.get(term) or self._loose_match(term, query_to_row)
            if row_id is None:
                continue
            person = person_by_row.get(row_id)
            if person is None:
                continue
            person_tokens = self._person_tokens(person)
            target_domain = (person.school_domain or "").lower()

            organic = item.get("organicResults") or []
            page_fetch_count = 0
            for result in organic:
                url = result.get("url") or ""
                title = result.get("title") or ""
                description = result.get("description") or ""
                blob = " ".join([title, description])

                for raw in EMAIL_RE.findall(blob):
                    email = raw.lower().strip(".,;:()<>[]{}\"' ")
                    if not self._is_usable(email):
                        continue
                    self._merge(
                        per_person[row_id],
                        email,
                        person_tokens,
                        target_domain,
                        source_url=url,
                        query=term,
                        origin="snippet",
                    )

                if (
                    page_fetch_count < self.fetch_top_pages
                    and not EMAIL_RE.search(blob)
                    and url
                ):
                    followups.append((row_id, url))
                    page_fetch_count += 1

        if followups:
            await self._fetch_followups(followups, person_by_row, per_person)

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
                # Skip templates whose required slots aren't populated, to avoid
                # producing nonsense like '"Jane Doe" ""  email'.
                needed = {f for _, f, _, _ in self._iter_format_fields(tmpl) if f}
                if any(not ctx.get(f) for f in needed):
                    continue
                q = tmpl.format(**ctx).strip()
                # Collapse double spaces from empty fields, just in case
                q = re.sub(r"\s{2,}", " ", q)
                if q and q not in out:
                    out.append(q)
            except (KeyError, IndexError):
                continue
        return out

    @staticmethod
    def _iter_format_fields(tmpl: str):
        import string
        return string.Formatter().parse(tmpl)

    @staticmethod
    def _is_usable(email: str) -> bool:
        if "@" not in email:
            return False
        local = email.split("@", 1)[0].lower()
        return local not in ROLE_LOCAL_PARTS

    @staticmethod
    def _loose_match(term: str, query_to_row: dict[str, str]) -> str | None:
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
        domain_match = (
            bool(target_domain) and dom.endswith(target_domain.lstrip("."))
        )
        # Personal email providers: stronger signal when name matches.
        is_personal_provider = dom in {
            "gmail.com",
            "outlook.com",
            "hotmail.com",
            "yahoo.com",
            "icloud.com",
            "me.com",
            "proton.me",
            "protonmail.com",
        }
        if token_match and domain_match:
            return Confidence.MEDIUM, "broad_serp: name+school both match"
        if token_match and is_personal_provider:
            return (
                Confidence.LOW,
                "broad_serp: name matches local-part on personal-email provider",
            )
        if token_match:
            return Confidence.LOW, "broad_serp: local-part matches name"
        if domain_match:
            return Confidence.LOW, "broad_serp: domain matches school but local-part doesn't"
        return Confidence.SPECULATIVE, "broad_serp: weak match (no name/domain overlap)"

    def _merge(
        self,
        bag: dict[str, EmailCandidate],
        email: str,
        person_tokens: set[str],
        target_domain: str,
        source_url: str,
        query: str,
        origin: str,
    ) -> None:
        confidence, base_note = self._score(email, person_tokens, target_domain)
        note = f"{base_note} ({origin})"
        existing = bag.get(email)
        if existing is None:
            bag[email] = EmailCandidate(
                email=email,
                confidence=confidence,
                source_provider=self.name,
                verified=False,
                notes=note,
                raw={"source_url": source_url, "query": query, "via": origin},
            )
            return
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
        followups: list[tuple[str, str]],
        person_by_row: dict[str, Person],
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
                        if r.status_code >= 400:
                            return
                        text = r.text
                    except Exception:
                        return
                    person = person_by_row.get(row_id)
                    if person is None:
                        return
                    person_tokens = self._person_tokens(person)
                    target_domain = (person.school_domain or "").lower()
                    for raw in EMAIL_RE.findall(text):
                        email = raw.lower().strip(".,;:()<>[]{}\"' ")
                        if not self._is_usable(email):
                            continue
                        self._merge(
                            per_person[row_id],
                            email,
                            person_tokens,
                            target_domain,
                            source_url=url,
                            query="(page fetch)",
                            origin="page",
                        )

            await asyncio.gather(
                *(_one(rid, url) for rid, url in followups),
                return_exceptions=True,
            )
