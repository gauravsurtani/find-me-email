"""On-demand single-URL email reveal — the addon API for plugging into a
larger application (candidate drawer, outreach tool, CRM enrichment, etc.).

Built on top of `find_emails.py` (Apify harvestapi) and `find_emails_signalhire.py`
(SignalHire). Both libraries can be used standalone for batch work; `reveal.py`
adds a thin orchestration layer designed for the per-click reveal pattern.

Design:
    • Caller passes an *ordered list of sources* — caller is in control.
    • Default: just Apify (cheap, ~$0.01).
    • To escalate (e.g., user marked Apify result as incorrect):
          reveal_email(url, sources=["signalhire"])
    • To run a full cascade (rare; usually you let the user trigger
      escalation per-click):
          reveal_email(url, sources=["apify_harvestapi", "signalhire"])

Returns a uniform `RevealResult` regardless of which provider answered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import find_emails as apify_provider
import find_emails_signalhire as signalhire_provider


# Per-call cost estimates, used to populate RevealResult.cost_usd. These are
# upper-bound rules of thumb (the actual provider invoice may be lower for
# non-billing outcomes). Adjust if your contracted rate differs.
COST_PER_CALL = {
    "apify_harvestapi": 0.010,   # $10 per 1k profiles, pay-per-result
    "signalhire":       0.060,   # $57/mo for 1k credits
}

VALID_SOURCES = tuple(COST_PER_CALL.keys())


@dataclass
class RevealResult:
    """Uniform return shape across all providers."""

    email: str | None = None              # best email found, or None
    alt_email: str | None = None          # second-best email, if any
    phones: list[str] = field(default_factory=list)  # SignalHire only
    source: str = "none"                  # which provider hit; "none" = miss
    cost_usd: float = 0.0                 # per-call cost
    status: str = ""                      # 'valid' | 'risky' | 'personal' | ...
    all_emails: list[str] = field(default_factory=list)  # everything returned
    profile: dict[str, Any] = field(default_factory=dict)  # name, headline, company

    @property
    def found(self) -> bool:
        return bool(self.email)


def reveal_email(
    linkedin_url: str,
    *,
    sources: Sequence[str] = ("apify_harvestapi",),
    apify_token: str | None = None,
    signalhire_api_key: str | None = None,
) -> RevealResult:
    """Look up an email for a LinkedIn URL using the requested sources, in order.

    Args:
        linkedin_url:        the LinkedIn profile URL.
        sources:             ordered list of providers to try. Returns on the
                             first hit. Defaults to Apify only.
                             Valid: "apify_harvestapi", "signalhire".
        apify_token:         override APIFY_TOKEN env var.
        signalhire_api_key:  override SIGNALHIRE_API_KEY env var.

    Returns:
        RevealResult — `result.found` is True if any provider returned an email.
    """
    if not linkedin_url or not str(linkedin_url).strip():
        return RevealResult()

    invalid = [s for s in sources if s not in VALID_SOURCES]
    if invalid:
        raise ValueError(
            f"unknown source(s): {invalid}. valid: {list(VALID_SOURCES)}"
        )

    for src in sources:
        if src == "apify_harvestapi":
            r = _try_apify(linkedin_url, apify_token)
        elif src == "signalhire":
            r = _try_signalhire(linkedin_url, signalhire_api_key)
        else:
            continue
        if r.found:
            return r

    return RevealResult()


def _try_apify(url: str, token: str | None) -> RevealResult:
    df = apify_provider.find_emails([url], apify_token=token, progress=False)
    if df.empty:
        return RevealResult()
    row = df.iloc[0]
    if not row["email"]:
        return RevealResult()
    all_emails = _extract_first_field(row["all_emails"])
    return RevealResult(
        email=row["email"],
        alt_email=all_emails[1] if len(all_emails) > 1 else None,
        phones=[],
        source="apify_harvestapi",
        cost_usd=COST_PER_CALL["apify_harvestapi"],
        status=str(row.get("email_status", "") or ""),
        all_emails=all_emails,
        profile={
            "name": row.get("name", "") or "",
            "headline": row.get("headline", "") or "",
            "company": row.get("company", "") or "",
        },
    )


def _try_signalhire(url: str, key: str | None) -> RevealResult:
    df = signalhire_provider.find_emails([url], api_key=key, progress=False)
    if df.empty:
        return RevealResult()
    row = df.iloc[0]
    if not row["email"]:
        return RevealResult()
    all_emails = _extract_first_field(row["all_emails"])
    phones = [p.strip() for p in str(row.get("phones") or "").split(";")
              if p.strip()]
    return RevealResult(
        email=row["email"],
        alt_email=all_emails[1] if len(all_emails) > 1 else None,
        phones=phones,
        source="signalhire",
        cost_usd=COST_PER_CALL["signalhire"],
        status=str(row.get("email_status", "") or ""),
        all_emails=all_emails,
        profile={
            "name": row.get("name", "") or "",
            "headline": row.get("headline", "") or "",
            "company": row.get("company", "") or "",
        },
    )


def _extract_first_field(all_emails_col: str) -> list[str]:
    """Both providers store all_emails as a semicolon-separated string. Apify
    rows are plain emails; SignalHire rows are `email|subType|rating`. Take the
    first pipe-field of each entry, return as a list."""
    if not all_emails_col:
        return []
    out: list[str] = []
    for chunk in str(all_emails_col).split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        out.append(chunk.split("|", 1)[0].strip())
    return [e for e in out if e]
