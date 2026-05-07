"""Email verification step (post-pass).

Takes the candidates accumulated by the multi-pass cascade, dedupes their
emails, submits them to the Million Verifier Apify actor, then writes the
verification result back onto each `EmailCandidate`:

  - `verified=True` for `result == "ok"` (mailbox confirmed)
  - `verified=False` plus a `notes` annotation for catch_all / unknown / error
  - SPECULATIVE candidates with `result == "ok"` are promoted to LOW (we have
    a verified mailbox now, but we still don't have a high-confidence reason
    to believe THIS specific person owns it)
  - SPECULATIVE candidates with `result == "invalid"` are dropped from the
    output (they bounce — no one owns this mailbox).

Why a separate module: verification operates on existing candidates rather
than on a Person, so it doesn't fit the EnrichmentProvider interface which
takes a Person and returns new candidates.

Recommended actor: `account56/email-verifier` (Million Verifier).
Pricing: $1 / 1000 decisive verifications; free on catch-all / unknown.
"""
from __future__ import annotations

from typing import Any

from rich.console import Console

from find_me_email.apify_client import ApifyClient
from find_me_email.schemas import Confidence, EmailCandidate, EnrichmentResult

console = Console()

# Result codes per Million Verifier docs:
RESULT_OK = "ok"
RESULT_CATCH_ALL = "catch_all"
RESULT_UNKNOWN = "unknown"
RESULT_ERROR = "error"
RESULT_DISPOSABLE = "disposable"
RESULT_INVALID = "invalid"


class EmailVerifier:
    """No-op base class. Subclass to plug in a real verifier."""

    name = "noop"

    async def verify(
        self, results: list[EnrichmentResult]
    ) -> list[EnrichmentResult]:
        return results


class ApifyMillionVerifier(EmailVerifier):
    """Verify emails via Apify's Million Verifier actor.

    Submits the union of all unverified candidate emails in one actor run
    (one billable run for the whole dataset, not per-row).
    """

    name = "apify_million_verifier"

    def __init__(self, config: dict[str, Any]):
        self.actor_id: str = config.get("actor_id") or "account56/email-verifier"
        self.timeout_s: int = int(config.get("timeout_s", 1800))
        # When `result == "ok"`, promote pure-SPECULATIVE candidates to LOW
        # (toggle via config: `promote_verified_speculative_to: low`).
        self.promote_speculative_to: str = config.get(
            "promote_verified_speculative_to", "low"
        )
        # When True, drop verified-INVALID candidates from output.
        self.drop_invalid: bool = bool(config.get("drop_invalid", True))
        # Skip already-verified candidates (saves money on re-runs).
        self.skip_already_verified: bool = bool(
            config.get("skip_already_verified", True)
        )

    async def verify(
        self, results: list[EnrichmentResult]
    ) -> list[EnrichmentResult]:
        # 1. Gather unique emails to check.
        emails_to_check: set[str] = set()
        for r in results:
            for c in r.candidates:
                if not c.email or "@" not in c.email:
                    continue
                if self.skip_already_verified and c.verified:
                    continue
                emails_to_check.add(c.email.lower())

        if not emails_to_check:
            console.print("[dim]verifier: no unverified candidates to check[/dim]")
            return results

        console.print(
            f"[cyan]→ verifier ({self.name})[/cyan] "
            f"checking {len(emails_to_check)} unique emails"
        )

        # 2. Run the actor.
        payload = {"emails": sorted(emails_to_check)}
        async with ApifyClient() as ac:
            items = await ac.run_actor_sync(
                self.actor_id, payload, wait_secs=self.timeout_s
            )

        # 3. Index results by email (lowercased).
        verdict: dict[str, dict[str, Any]] = {}
        for item in items:
            email = (item.get("email") or "").strip().lower()
            if email:
                verdict[email] = item
                # Normalize result field across actor schemas.
                # account56/email-verifier uses `result` (ok/invalid/catch_all/...).
                # michael.g/email-verifier-validator uses `status` (valid/invalid/risky)
                # plus `technical_status` (ok/catch_all/...).
                if "result" not in item:
                    item["result"] = self._normalize_result(item)

        # 4. Apply verdicts to candidates.
        promoted = 0
        dropped = 0
        confirmed = 0
        for r in results:
            keep: list[EmailCandidate] = []
            for c in r.candidates:
                v = verdict.get(c.email.lower()) if c.email else None
                if v is None:
                    keep.append(c)
                    continue
                result_code = (v.get("result") or "").lower()
                annotation = self._build_note(v)

                if result_code == RESULT_OK:
                    c.verified = True
                    c.verification_method = "smtp_million_verifier"
                    c.notes = (c.notes + f" | verifier: {annotation}").strip(" |")
                    if c.confidence == Confidence.SPECULATIVE:
                        # Promote: a verified-OK pattern guess deserves LOW
                        # (we now know the mailbox EXISTS), but not MEDIUM
                        # (we still don't have direct evidence the person
                        # owns it).
                        target = (
                            Confidence.LOW
                            if self.promote_speculative_to == "low"
                            else Confidence.SPECULATIVE
                        )
                        c.confidence = target
                        promoted += 1
                    confirmed += 1
                    keep.append(c)
                elif result_code == RESULT_INVALID:
                    if self.drop_invalid:
                        dropped += 1
                        continue
                    c.notes = (c.notes + f" | verifier: {annotation}").strip(" |")
                    keep.append(c)
                else:
                    # catch_all / unknown / disposable / error: keep, annotate
                    c.notes = (c.notes + f" | verifier: {annotation}").strip(" |")
                    keep.append(c)
            r.candidates = keep
            if self.name not in r.providers_attempted:
                r.providers_attempted.append(self.name)

        console.print(
            f"  [dim]verifier results: {confirmed} confirmed, "
            f"{promoted} speculative promoted, {dropped} invalid dropped[/dim]"
        )
        return results

    @staticmethod
    def _build_note(v: dict[str, Any]) -> str:
        bits = [f"result={v.get('result', '?')}"]
        if v.get("quality"):
            bits.append(f"quality={v['quality']}")
        if v.get("subresult"):
            bits.append(f"detail={v['subresult']}")
        if v.get("status") and v.get("status") != v.get("result"):
            bits.append(f"status={v['status']}")
        if v.get("technical_status"):
            bits.append(f"tech={v['technical_status']}")
        if v.get("score") is not None:
            bits.append(f"score={v['score']}")
        if v.get("role"):
            bits.append("role=true")
        if v.get("free"):
            bits.append("free=true")
        return ",".join(bits)

    @staticmethod
    def _normalize_result(item: dict[str, Any]) -> str:
        """Map michael.g schema to canonical {ok|invalid|catch_all|unknown}.

        Other actors that emit `result` directly bypass this.
        """
        status = (item.get("status") or "").lower()
        technical = (item.get("technical_status") or "").lower()
        verdict = (item.get("verification_details") or {}).get("verdict") or {}
        is_valid = verdict.get("is_valid")

        if is_valid is True or status == "valid":
            # michael.g flags catch-all as `risky` not `valid`, but be defensive
            if technical == "catch_all" or item.get("catch_all"):
                return RESULT_CATCH_ALL
            return RESULT_OK
        if status == "invalid" or is_valid is False and technical not in {"catch_all"}:
            # `risky` with catch_all should NOT be marked invalid
            if status == "risky" and (technical == "catch_all" or item.get("catch_all")):
                return RESULT_CATCH_ALL
            if status == "invalid":
                return RESULT_INVALID
        if technical == "catch_all" or item.get("catch_all"):
            return RESULT_CATCH_ALL
        if item.get("disposable"):
            return RESULT_DISPOSABLE
        if status == "risky":
            return RESULT_UNKNOWN
        return RESULT_UNKNOWN


def build_verifier(cfg: dict[str, Any]) -> EmailVerifier | None:
    """Factory. Returns None if verifier is disabled / unconfigured."""
    if not cfg:
        return None
    actor_id = cfg.get("actor_id") or ""
    if not actor_id:
        return None
    name = cfg.get("name") or "apify_million_verifier"
    if name == "apify_million_verifier" or "verifier" in actor_id.lower():
        return ApifyMillionVerifier(cfg)
    raise ValueError(f"Unknown verifier: {name}")
