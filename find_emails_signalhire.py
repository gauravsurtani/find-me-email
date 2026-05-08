"""Find emails for LinkedIn profile URLs via SignalHire's Person API.

Single-file, dependency-light. Drop this file into any Python project.
Mirrors the shape of find_emails.py (which uses Apify harvestapi) so the two
can be composed in a cascade.

Requirements:
    pip install httpx pandas python-dotenv

Setup:
    export SIGNALHIRE_API_KEY=...        # get from signalhire.com/api

Usage as a Python library:

    from find_emails_signalhire import find_emails, find_email

    # Single:
    email = find_email("https://linkedin.com/in/lvblack")

    # Batch:
    df = find_emails([
        "https://linkedin.com/in/lvblack",
        "https://linkedin.com/in/naomiwong19",
    ])
    # df columns: linkedin_url, email, all_emails, email_status,
    #             email_quality, name, headline, company, phones, raw

Why use this when find_emails.py exists:
    Apify harvestapi (find_emails.py) hits ~50% on student/personal emails.
    SignalHire's contributory network finds personal emails (Gmail, .edu)
    that LinkedIn's DB doesn't expose — pushes coverage to ~80% on
    LinkedIn-public profiles.

    Cost: ~$0.06 per credit (SignalHire Emails plan, $57/mo for 1000).
    Empirically ~0.5-0.7 credits per lookup, so ~$0.03/lookup average.

API notes:
    Endpoint: POST https://www.signalhire.com/api/v1/candidate/search
    Auth:     `apikey: <KEY>` header
    Sync mode: pass `withoutWaterfall: true` (no callback URL needed).
    Batch:    up to 100 LinkedIn URLs per request.
    Returns:  per-item candidate object with `contacts` array — each contact
              has `value`, `subType` ('personal' | 'work' | 'mobile' | ...),
              and `rating` (0-100).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Sequence

import httpx
import pandas as pd

ENDPOINT = "https://www.signalhire.com/api/v1/candidate/search"
DEFAULT_BATCH_SIZE = 50      # well under SignalHire's 100/request cap
DEFAULT_PARALLEL = 3         # be conservative; SignalHire rate-limits
DEFAULT_TIMEOUT_S = 120


# ─────────────────────────────────────────────────────────────────────────────
# Public API — mirrors find_emails.py
# ─────────────────────────────────────────────────────────────────────────────

def find_email(
    linkedin_url: str,
    *,
    api_key: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> str | None:
    """Best email for a single LinkedIn profile URL. None if not found."""
    df = find_emails(
        [linkedin_url],
        api_key=api_key,
        batch_size=1,
        parallel=1,
        timeout_s=timeout_s,
        progress=False,
    )
    if df.empty:
        return None
    email = df.iloc[0]["email"]
    return email or None


def find_emails(
    linkedin_urls: Sequence[str],
    *,
    api_key: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    parallel: int = DEFAULT_PARALLEL,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    progress: bool = True,
) -> pd.DataFrame:
    """Look up emails for a batch of LinkedIn URLs via SignalHire.

    Returns a DataFrame with one row per input URL (in original order). Columns:
        linkedin_url, email, all_emails, email_status, email_quality,
        name, headline, company, phones, raw

    `email` is the best candidate (prefers subType='personal', then 'work',
        ranked by rating).
    `email_status` is the subType of the best email ('personal' / 'work' / etc).
    `email_quality` is the rating (0–100).
    `all_emails` is "email|subType|rating; ..." for every email returned.
    `phones` is a semicolon-separated list of phone numbers (also returned by
        SignalHire as a bonus on personal-email lookups).
    `raw` is the JSON-encoded full candidate response (full LinkedIn profile).
    """
    token = api_key or os.environ.get("SIGNALHIRE_API_KEY")
    if not token:
        raise ValueError(
            "SignalHire API key required. Pass api_key=... or set "
            "SIGNALHIRE_API_KEY env var."
        )

    urls = _normalize_urls(linkedin_urls)
    return asyncio.run(
        _find_emails_async(urls, token, batch_size, parallel, timeout_s, progress)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_urls(urls: Sequence[str]) -> list[str]:
    out: list[str] = []
    for u in urls:
        if u is None:
            continue
        u = str(u).strip()
        if not u:
            continue
        if not u.startswith(("http://", "https://")):
            u = f"https://{u}"
        out.append(u)
    return out


async def _find_emails_async(
    urls: list[str], token: str, batch_size: int, parallel: int,
    timeout_s: int, progress: bool,
) -> pd.DataFrame:
    if not urls:
        return _empty_frame()

    batches = [urls[i : i + batch_size] for i in range(0, len(urls), batch_size)]
    if progress:
        print(f"signalhire: {len(urls)} URLs in {len(batches)} batches "
              f"(<= {batch_size} each), parallel={parallel}")

    sem = asyncio.Semaphore(parallel)
    results_by_slug: dict[str, dict] = {}
    t0 = time.time()
    credits_left: str | None = None

    async with httpx.AsyncClient(timeout=timeout_s + 30) as client:

        async def process_batch(idx: int, batch: list[str]) -> None:
            nonlocal credits_left
            async with sem:
                t1 = time.time()
                try:
                    items, headers = await _call_api(client, batch, token, timeout_s)
                except Exception as e:
                    if progress:
                        print(f"  batch {idx + 1}/{len(batches)}: ERROR "
                              f"{type(e).__name__}: {str(e)[:120]}")
                    return
                cl = headers.get("x-credits-left")
                if cl:
                    credits_left = cl
                for item in items:
                    if item.get("status") != "success":
                        continue
                    slug = _slug_from_url(item.get("item", ""))
                    if not slug:
                        continue
                    cand = item.get("candidate") or {}
                    if cand:
                        results_by_slug[slug] = cand
                if progress:
                    n_with = sum(
                        1 for it in items
                        if it.get("status") == "success"
                        and (it.get("candidate") or {}).get("contacts")
                    )
                    print(f"  batch {idx + 1}/{len(batches)}: "
                          f"{len(items)} returned, {n_with} with contacts "
                          f"({time.time() - t1:.0f}s)")

        await asyncio.gather(*[process_batch(i, b) for i, b in enumerate(batches)])

    if progress:
        msg = f"signalhire: done in {time.time() - t0:.0f}s"
        if credits_left is not None:
            msg += f" — credits left: {credits_left}"
        print(msg)

    return _build_dataframe(urls, results_by_slug)


async def _call_api(
    client: httpx.AsyncClient, urls: list[str], token: str, timeout_s: int,
) -> tuple[list[dict], dict[str, str]]:
    """POST sync (withoutWaterfall) request. Returns (items, response_headers)."""
    body = {"items": urls, "withoutWaterfall": True}
    r = await client.post(
        ENDPOINT,
        headers={"apikey": token, "Content-Type": "application/json"},
        json=body,
        timeout=timeout_s,
    )
    r.raise_for_status()
    return (r.json() or []), dict(r.headers)


def _slug_from_url(url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", (url or "").lower())
    return m.group(1) if m else ""


# Email-type ranking: personal beats work, work beats anything else.
_EMAIL_TYPE_RANK = {"personal": 0, "work": 1}


def _extract_emails(candidate: dict) -> list[dict]:
    """Pull email-type contacts from a candidate, sorted best-first.

    Returns dicts: {value, subType, rating}.  Sort: personal > work > other,
    then highest rating.
    """
    contacts = candidate.get("contacts") or []
    emails = [
        c for c in contacts
        if (c.get("type") == "email") and c.get("value")
        and "@" in str(c["value"])
    ]
    emails.sort(key=lambda c: (
        _EMAIL_TYPE_RANK.get((c.get("subType") or "").lower(), 9),
        -(c.get("rating") or 0),
    ))
    return emails


def _extract_phones(candidate: dict) -> list[str]:
    contacts = candidate.get("contacts") or []
    return [
        str(c.get("value", "")).strip()
        for c in contacts
        if (c.get("type") in ("phone", "mobile_phone")
            or "phone" in (c.get("subType", "") or "").lower())
        and c.get("value")
    ]


def _current_company(candidate: dict) -> str:
    for e in candidate.get("experience") or []:
        if e.get("current"):
            return e.get("company") or ""
    exp = candidate.get("experience") or []
    if exp:
        return exp[0].get("company") or ""
    return ""


def _build_dataframe(urls: list[str], results: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for url in urls:
        slug = _slug_from_url(url)
        cand = results.get(slug, {})
        emails = _extract_emails(cand)
        best = emails[0] if emails else {}
        all_emails_str = "; ".join(
            f"{e.get('value','')}|{e.get('subType','')}|{e.get('rating','')}"
            for e in emails
        )
        phones = _extract_phones(cand)
        rows.append({
            "linkedin_url": url,
            "email": best.get("value", "") or "",
            "all_emails": all_emails_str,
            "email_status": (best.get("subType") or "") if best else "",
            "email_quality": best.get("rating", "") if best else "",
            "name": cand.get("fullName", "") or "",
            "headline": cand.get("headLine", "") or "",
            "company": _current_company(cand) if cand else "",
            "phones": "; ".join(phones),
            "raw": json.dumps(cand, default=str) if cand else "",
        })
    return pd.DataFrame(rows)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "linkedin_url", "email", "all_emails", "email_status",
        "email_quality", "name", "headline", "company", "phones", "raw",
    ])


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Find emails for LinkedIn profile URLs via SignalHire. "
            "Reads a CSV, writes a CSV with email columns appended."
        ),
    )
    ap.add_argument("input_csv", help="Path to input CSV file")
    ap.add_argument("--url-column", default="linkedin_url",
                    help="Column name containing LinkedIn URLs")
    ap.add_argument("--output", default="emails_signalhire.csv",
                    help="Output CSV path")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL)
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N rows (dry run)")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except ImportError:
        pass

    df_in = pd.read_csv(args.input_csv)
    if args.url_column not in df_in.columns:
        print(f"ERROR: column '{args.url_column}' not in input.", file=sys.stderr)
        print(f"Available: {list(df_in.columns)}", file=sys.stderr)
        return 2
    if args.limit:
        df_in = df_in.head(args.limit)

    urls = df_in[args.url_column].dropna().astype(str).tolist()
    print(f"Reading {len(urls)} URLs from {args.input_csv}")

    df = find_emails(urls, batch_size=args.batch_size, parallel=args.parallel)

    if len(df) == len(df_in):
        for col in df_in.columns:
            if col not in df.columns and col != args.url_column:
                df[col] = df_in[col].values

    df.to_csv(args.output, index=False)
    n_email = (df["email"] != "").sum()
    print(f"Wrote {args.output}: {len(df)} rows, "
          f"{n_email} ({100 * n_email / max(1, len(df)):.1f}%) with email")
    return 0


if __name__ == "__main__":
    sys.exit(main())
