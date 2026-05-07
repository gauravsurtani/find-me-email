from __future__ import annotations

import re

from find_me_email.college_domains import domains_for, resolve_domain
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import Confidence, EmailCandidate, Person

# Patterns are tried against EVERY domain candidate for the school (primary + subdomains).
# `{first}` / `{last}` may include a middle name when present (see _name_parts).
DEFAULT_PATTERNS = [
    "{first}.{last}@{domain}",
    "{first}{last}@{domain}",
    "{f}{last}@{domain}",
    "{first}{l}@{domain}",          # snigdhag (first + initial of last)
    "{first}_{last}@{domain}",
    "{first}@{domain}",
    "{last}.{first}@{domain}",
    # Multi-word-name variants (only meaningful when {middle} is present)
    "{first}{middle}.{last}@{domain}",
    "{first}{middle}{last}@{domain}",
    "{first}.{middle}.{last}@{domain}",
    "{first}_{middle}_{last}@{domain}",
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
        first, _, last = self._name_parts(person)
        domain = person.school_domain or resolve_domain(person.school)
        return bool(first and last and domain)

    async def enrich(self, person: Person) -> list[EmailCandidate]:
        first, middle, last = self._name_parts(person)
        domains = self._domains_for(person)
        if not (first and last and domains):
            return []

        first = self._sanitize(first)
        last = self._sanitize(last)
        middle_s = self._sanitize(middle) if middle else ""

        out: list[EmailCandidate] = []
        seen: set[str] = set()
        for domain in domains:
            ctx = {
                "first": first,
                "last": last,
                "f": first[:1],
                "l": last[:1],
                "middle": middle_s,
                "m": middle_s[:1] if middle_s else "",
                "domain": domain,
            }
            for pat in self.patterns:
                # Skip middle-name patterns when there's no middle name
                if "{middle}" in pat and not middle_s:
                    continue
                try:
                    email = pat.format(**ctx).lower()
                except KeyError:
                    continue
                # Reject malformed (e.g. consecutive separators when middle is empty)
                if email in seen or "@" not in email or email.startswith(".") or ".." in email or "__" in email:
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
                        raw={
                            "pattern": pat,
                            "domain": domain,
                            "domain_source": "school_domain" if person.school_domain else "resolved",
                        },
                    )
                )
        return out

    @staticmethod
    def _domains_for(person: Person) -> list[str]:
        """If the row has an explicit school_domain, use only that; otherwise expand
        the school name to its primary + known subdomain variants."""
        if person.school_domain:
            return [person.school_domain]
        return domains_for(person.school)

    @staticmethod
    def _name_parts(person: Person) -> tuple[str | None, str | None, str | None]:
        """Return (first, middle_or_None, last). Middle is everything between first and last
        joined with no separator, so 'Kedar Prashant Vichare' → ('Kedar', 'Prashant', 'Vichare')."""
        if person.first_name and person.last_name:
            return person.first_name, None, person.last_name
        if person.name:
            parts = [p for p in person.name.strip().split() if p]
            if len(parts) == 2:
                return parts[0], None, parts[1]
            if len(parts) >= 3:
                return parts[0], "".join(parts[1:-1]), parts[-1]
        return None, None, None

    @staticmethod
    def _sanitize(s: str) -> str:
        return re.sub(r"[^a-z]", "", s.lower())
