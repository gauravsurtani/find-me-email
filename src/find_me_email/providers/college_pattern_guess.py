from __future__ import annotations

import re

from find_me_email.college_domains import resolve_domain
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import Confidence, EmailCandidate, Person

DEFAULT_PATTERNS = [
    "{first}.{last}@{domain}",
    "{first}{last}@{domain}",
    "{f}{last}@{domain}",
    "{first}_{last}@{domain}",
    "{first}@{domain}",
]


class CollegePatternGuessProvider(EnrichmentProvider):
    """Generates candidate .edu emails from common patterns.

    These are GUESSES. They are tagged Confidence.SPECULATIVE and the notes field
    carries an explicit warning so they are impossible to miss when reviewing output.
    """

    name = "college_pattern_guess"
    cost_per_call_usd = 0.0  # free; cost lives in the verifier

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.patterns: list[str] = self.config.get("patterns", DEFAULT_PATTERNS)

    def can_handle(self, person: Person) -> bool:
        first, last = self._split_name(person)
        domain = person.school_domain or resolve_domain(person.school)
        return bool(first and last and domain)

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        first, last = self._split_name(person)
        domain = person.school_domain or resolve_domain(person.school)
        if not (first and last and domain):
            return []

        first = self._sanitize(first)
        last = self._sanitize(last)
        ctx = {"first": first, "last": last, "f": first[:1], "l": last[:1], "domain": domain}

        out: list[EmailCandidate] = []
        seen: set[str] = set()
        for pat in self.patterns:
            try:
                email = pat.format(**ctx).lower()
            except KeyError:
                continue
            if email in seen:
                continue
            seen.add(email)
            out.append(
                EmailCandidate(
                    email=email,
                    confidence=Confidence.SPECULATIVE,
                    source_provider=self.name,
                    verified=False,
                    notes=(
                        "PATTERN GUESS — this email may not exist or may not reach the person. "
                        "Verify before outreach."
                    ),
                    raw={"pattern": pat, "domain_source": "school_domain" if person.school_domain else "resolved"},
                )
            )
        return out

    @staticmethod
    def _split_name(person: Person) -> tuple[str | None, str | None]:
        if person.first_name and person.last_name:
            return person.first_name, person.last_name
        if person.name:
            parts = person.name.strip().split()
            if len(parts) >= 2:
                return parts[0], parts[-1]
        return None, None

    @staticmethod
    def _sanitize(s: str) -> str:
        return re.sub(r"[^a-z]", "", s.lower())
