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
    cost_per_call_usd = 0.0  # Actor is $0 per event (consumes platform compute credits only)

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
        row_to_person: dict[str, Person] = {p.row_id: p for p in targets}
        payload = {"profileUrls": list(url_to_row.keys())}

        async with ApifyClient() as ac:
            items = await ac.run_actor_sync(self.actor_id, payload, wait_secs=self.timeout_s)

        out: dict[str, list[EmailCandidate]] = {p.row_id: [] for p in people}
        for item in items:
            row_id = self._match_row(item, url_to_row)
            if not row_id:
                continue
            # Side effect: enrich the Person with school context discovered in the
            # actor's profile response. Lets downstream pattern_guess fire even when
            # the source CSV had no school column.
            person = row_to_person.get(row_id)
            if person is not None:
                self._enrich_person_from_item(item, person)
            for cand in self._extract_emails(item):
                cand.source_provider = self.name
                cand.cost_usd = self.cost_per_call_usd
                out[row_id].append(cand)
        return out

    def _match_row(self, item: dict[str, Any], url_to_row: dict[str, str]) -> str | None:
        # b2b_leads echoes the input URL in `url`. harvestapi uses `profileUrl`/`linkedinUrl`.
        for k in ("url", "profileUrl", "linkedinUrl", "input", "linkedin_url"):
            v = item.get(k)
            if isinstance(v, str):
                # Normalize www. and trailing slash for matching
                normalized = v.rstrip("/").replace("https://www.", "https://")
                for url, rid in url_to_row.items():
                    if normalized.endswith(url.rstrip("/").replace("https://www.", "https://").split("://", 1)[-1]):
                        return rid
                if v in url_to_row:
                    return url_to_row[v]
        # Last resort: search any string value for any input URL
        for url, rid in url_to_row.items():
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            for v in item.values():
                if isinstance(v, str) and slug in v:
                    return rid
        return None

    @staticmethod
    def _enrich_person_from_item(item: dict[str, Any], person: Person) -> None:
        """Populate person.school / school_domain from the actor's profile response.

        Looks at the structured `educations` field first (most reliable). Skips
        masked/redacted entries (harvestapi sometimes returns asterisks for
        unverified/private fields). If multiple schools, picks the most recent
        one with a non-masked name.
        """
        if person.school and person.school_domain:
            return
        from find_me_email.college_domains import resolve_domain

        edus = item.get("educations") or item.get("education") or []
        if isinstance(edus, dict):
            edus = [edus]

        # Prefer entries with the most recent end_date (or no end_date = current student)
        def _sort_key(e: dict) -> tuple:
            end = (e.get("end_date") or "9999")[:7]  # current first
            start = (e.get("start_date") or "0000")[:7]
            return (end, start)

        sortable = [e for e in edus if isinstance(e, dict)]
        sortable.sort(key=_sort_key, reverse=True)

        for edu in sortable:
            sch = (edu.get("school") or edu.get("schoolName") or "").strip()
            if not sch or "*" in sch:  # masked/redacted
                continue
            person.school = person.school or sch
            if not person.school_domain:
                person.school_domain = resolve_domain(sch)
            return

        # Fallback: if no education info, try the headline
        if not person.school:
            from find_me_email.college_domains import extract_school_from_text
            inferred = extract_school_from_text(item.get("headline"), item.get("about"))
            if inferred:
                person.school = inferred
                person.school_domain = resolve_domain(inferred)

    @staticmethod
    def _confidence_from_str(s: str | None) -> Confidence:
        return {
            "high": Confidence.MEDIUM,        # DB match, unverified by us — caller may verify
            "medium": Confidence.MEDIUM,
            "low": Confidence.SPECULATIVE,
            "speculative": Confidence.SPECULATIVE,
        }.get((s or "").lower(), Confidence.MEDIUM)

    def _extract_emails(self, item: dict[str, Any]) -> list[EmailCandidate]:
        candidates: list[EmailCandidate] = []
        seen: set[str] = set()

        # b2b_leads schema: emails is a list of {email, source, confidence, verified_on_platforms}
        for entry in item.get("emails") or []:
            if not isinstance(entry, dict):
                continue
            email = (entry.get("email") or "").strip().lower()
            if not email or "@" not in email or email in seen:
                continue
            seen.add(email)
            verified_platforms = entry.get("verified_on_platforms") or []
            verified = bool(verified_platforms)
            source = entry.get("source") or "unknown"
            note = f"b2b_leads via {source}"
            if verified_platforms:
                note += f" (verified on {','.join(verified_platforms)})"
            cand = EmailCandidate(
                email=email,
                confidence=self._confidence_from_str(entry.get("confidence")),
                source_provider=self.name,
                verified=verified,
                verification_method=",".join(verified_platforms) if verified_platforms else None,
                notes=note,
                raw=entry,
            )
            # Promote best_email to top of list
            if email == (item.get("best_email") or "").strip().lower():
                candidates.insert(0, cand)
            else:
                candidates.append(cand)

        # Fallback: top-level scalar fields (some actors return these)
        for k in ("email", "emailAddress", "workEmail", "personalEmail"):
            v = item.get(k)
            if isinstance(v, str) and "@" in v and v.strip().lower() not in seen:
                e = v.strip().lower()
                seen.add(e)
                candidates.append(
                    EmailCandidate(
                        email=e,
                        confidence=Confidence.MEDIUM,
                        source_provider=self.name,
                        notes=f"top-level {k}",
                        raw={k: v},
                    )
                )
        return candidates
