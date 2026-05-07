"""Shared email-matching utilities used by every provider.

Centralizes:
  - Email regex (`EMAIL_RE` for finding inside text, `EMAIL_FULLMATCH_RE` for validating)
  - Role-account local-part filter
  - Person → token set extraction (for matching name fragments to email local-parts)
  - Score logic (token + domain match → Confidence)
  - Candidate-bag merge with confidence promotion
  - Apify-actor query-attribution helper

Providers wrap these with their own notes/origin labels — keep provider files
focused on data-source-specific quirks, not on email-matching mechanics.
"""
from __future__ import annotations

import re

from find_me_email.schemas import Confidence, EmailCandidate, Person

# Unanchored: use with .findall on text blobs (snippets, page bodies).
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Anchored: use with .match for strict validation of a candidate string.
EMAIL_FULLMATCH_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# Role-account local-parts that show up in scraped HTML but rarely belong to
# the target person. Providers may extend this set with provider-specific
# additions (CI bots, etc.).
ROLE_LOCAL_PARTS: frozenset[str] = frozenset(
    {
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
        "abuse",
    }
)

PERSONAL_PROVIDER_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "outlook.com",
        "hotmail.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "proton.me",
        "protonmail.com",
    }
)


def person_tokens(person: Person) -> set[str]:
    """Extract 3+ char alphanumeric tokens from name fields, lowercased."""
    tokens: set[str] = set()
    for s in (person.name, person.first_name, person.last_name):
        if not s:
            continue
        for t in re.split(r"[^a-z]+", s.lower()):
            if len(t) >= 3:
                tokens.add(t)
    return tokens


def normalize_email(raw: str) -> str:
    """Lowercase + strip surrounding punctuation/quotes from a regex match."""
    return raw.lower().strip(".,;:()<>[]{}\"' ")


def is_usable_local_part(email: str) -> bool:
    """Filter out role-account locals (info@, support@, etc.)."""
    if "@" not in email:
        return False
    local = email.split("@", 1)[0].lower()
    return local not in ROLE_LOCAL_PARTS


def match_flags(
    email: str, tokens: set[str], target_domain: str
) -> tuple[bool, bool]:
    """Return (token_match, domain_match) for an email against a person.

    `token_match` = the local-part contains any of the person's name tokens.
    `domain_match` = the domain ends with the target school/work domain.
    """
    local, _, dom = email.partition("@")
    token_match = (
        any(tok in local.lower() for tok in tokens) if tokens else False
    )
    domain_match = (
        bool(target_domain) and dom.endswith(target_domain.lstrip("."))
    )
    return token_match, domain_match


def merge_candidate(
    bag: dict[str, EmailCandidate],
    *,
    email: str,
    confidence: Confidence,
    notes: str,
    source_provider: str,
    raw: dict | None = None,
) -> None:
    """Insert or promote a candidate in `bag` (keyed by email).

    A new sighting only overwrites existing confidence/notes if it ranks
    higher (HIGH < MEDIUM < LOW < SPECULATIVE).
    """
    if not email or "@" not in email:
        return
    existing = bag.get(email)
    if existing is None:
        bag[email] = EmailCandidate(
            email=email,
            confidence=confidence,
            source_provider=source_provider,
            verified=False,
            notes=notes,
            raw=raw,
        )
        return
    if confidence.rank < existing.confidence.rank:
        existing.confidence = confidence
        existing.notes = notes


def loose_match_query(
    actor_term: str, query_to_row: dict[str, str]
) -> str | None:
    """Whitespace-insensitive lookup for Apify-echoed query strings.

    The google-search-scraper actor sometimes collapses whitespace in the
    `searchQuery.term` field, which breaks exact dict lookups.
    """
    norm = re.sub(r"\s+", " ", actor_term.strip().lower())
    for q, rid in query_to_row.items():
        if re.sub(r"\s+", " ", q.strip().lower()) == norm:
            return rid
    return None
