"""Post-pass email verification.

Submits all unverified candidate emails to a single Apify actor run, then
writes the verdict back onto each `EmailCandidate`. Operates on existing
candidates rather than on a `Person`, so it doesn't fit the
`EnrichmentProvider` interface.
"""
from __future__ import annotations

from typing import Any

from rich.console import Console

from find_me_email.apify_client import ApifyClient
from find_me_email.schemas import Confidence, EmailCandidate, EnrichmentResult

console = Console()

RESULT_OK = "ok"
RESULT_CATCH_ALL = "catch_all"
RESULT_UNKNOWN = "unknown"
RESULT_ERROR = "error"
RESULT_DISPOSABLE = "disposable"
RESULT_INVALID = "invalid"


class EmailVerifier:
    """No-op base. Subclass to plug in a real verifier."""

    name = "noop"
    cost_per_call_usd: float = 0.0

    async def verify(
        self, results: list[EnrichmentResult]
    ) -> list[EnrichmentResult]:
        return results


class ApifyMillionVerifier(EmailVerifier):
    """Million Verifier on Apify (`michael.g/email-verifier-validator`).

    Default actor pricing is ~$0.60 / 1000 decisive verifications.
    """

    name = "apify_million_verifier"

    def __init__(self, config: dict[str, Any]):
        self.actor_id: str = config.get("actor_id") or "michael.g/email-verifier-validator"
        self.timeout_s: int = int(config.get("timeout_s", 1800))
        # Per-decisive-result rate; budget tracking multiplies this by checked count.
        self.cost_per_call_usd = float(config.get("cost_per_email_usd", 0.0006))
        self.promote_speculative_to: str = config.get(
            "promote_verified_speculative_to", "low"
        )
        self.drop_invalid: bool = bool(config.get("drop_invalid", True))
        self.skip_already_verified: bool = bool(config.get("skip_already_verified", True))

    async def verify(
        self, results: list[EnrichmentResult]
    ) -> list[EnrichmentResult]:
        emails_to_check = self._collect_emails(results)
        if not emails_to_check:
            console.print("[dim]verifier: no unverified candidates to check[/dim]")
            return results

        console.print(
            f"[cyan]→ verifier ({self.name})[/cyan] "
            f"checking {len(emails_to_check)} unique emails"
        )

        async with ApifyClient() as ac:
            items = await ac.run_actor_sync(
                self.actor_id,
                {"emails": sorted(emails_to_check)},
                wait_secs=self.timeout_s,
            )

        verdict = self._index_verdicts(items)
        confirmed, promoted, dropped = self._apply_verdicts(results, verdict)
        console.print(
            f"  [dim]verifier: {confirmed} confirmed, "
            f"{promoted} speculative promoted, {dropped} invalid dropped[/dim]"
        )
        return results

    def _collect_emails(self, results: list[EnrichmentResult]) -> set[str]:
        out: set[str] = set()
        for r in results:
            for c in r.candidates:
                if not c.email or "@" not in c.email:
                    continue
                if self.skip_already_verified and c.verified:
                    continue
                out.add(c.email.lower())
        return out

    def _index_verdicts(
        self, items: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for item in items:
            email = (item.get("email") or "").strip().lower()
            if not email:
                continue
            if "result" not in item:
                item["result"] = self._normalize_result(item)
            out[email] = item
        return out

    def _apply_verdicts(
        self,
        results: list[EnrichmentResult],
        verdict: dict[str, dict[str, Any]],
    ) -> tuple[int, int, int]:
        confirmed = promoted = dropped = 0
        for r in results:
            keep: list[EmailCandidate] = []
            for c in r.candidates:
                v = verdict.get(c.email.lower()) if c.email else None
                if v is None:
                    keep.append(c)
                    continue
                code = (v.get("result") or "").lower()
                annotation = self._build_note(v)

                if code == RESULT_OK:
                    c.verified = True
                    c.verification_method = "smtp_million_verifier"
                    c.notes = (c.notes + f" | verifier: {annotation}").strip(" |")
                    if c.confidence == Confidence.SPECULATIVE and self.promote_speculative_to == "low":
                        c.confidence = Confidence.LOW
                        promoted += 1
                    confirmed += 1
                    keep.append(c)
                elif code == RESULT_INVALID and self.drop_invalid:
                    dropped += 1
                else:
                    c.notes = (c.notes + f" | verifier: {annotation}").strip(" |")
                    keep.append(c)
            r.candidates = keep
            if self.name not in r.providers_attempted:
                r.providers_attempted.append(self.name)
        return confirmed, promoted, dropped

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
        """Map michael.g schema (`status`/`technical_status`/`verification_details`)
        to canonical {ok|invalid|catch_all|disposable|unknown}.

        Catch-all takes precedence over status because every address at a
        catch-all domain accepts SMTP, which makes a `valid`/`is_valid=True`
        verdict misleading without that qualifier.
        """
        technical = (item.get("technical_status") or "").lower()
        if technical == "catch_all" or item.get("catch_all"):
            return RESULT_CATCH_ALL

        status = (item.get("status") or "").lower()
        verdict = (item.get("verification_details") or {}).get("verdict") or {}
        is_valid = verdict.get("is_valid")

        if status == "valid" or is_valid is True:
            return RESULT_OK
        if status == "invalid" or is_valid is False:
            return RESULT_INVALID
        if item.get("disposable"):
            return RESULT_DISPOSABLE
        return RESULT_UNKNOWN


def build_verifier(cfg: dict[str, Any]) -> EmailVerifier | None:
    """Returns None if verifier is disabled / unconfigured."""
    if not cfg or not cfg.get("actor_id"):
        return None
    name = cfg.get("name") or "apify_million_verifier"
    if name == "apify_million_verifier" or "verifier" in (cfg.get("actor_id") or "").lower():
        return ApifyMillionVerifier(cfg)
    raise ValueError(f"Unknown verifier: {name}")
