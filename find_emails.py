"""Find emails for LinkedIn profile URLs via Apify's harvestapi actor.

Single-file, dependency-light. Drop this file into any Python project.

Requirements:
    pip install httpx pandas python-dotenv

Setup:
    export APIFY_TOKEN=apify_api_xxx     # get from apify.com (FREE plan works)

Usage as a Python library:

    from find_emails import find_emails, find_email

    # One-shot:
    email = find_email("https://linkedin.com/in/satyanadella")

    # Batch:
    df = find_emails([
        "https://linkedin.com/in/satyanadella",
        "https://linkedin.com/in/jeffweiner08",
    ])
    # df columns: linkedin_url, email, all_emails, email_status,
    #             email_quality, name, headline, company, raw

Usage as a CLI:

    python find_emails.py people.csv \\
        --url-column linkedin_url \\
        --output emails.csv

    # Or, after `pip install -e .`, the same thing as a console script:
    find-emails people.csv --url-column linkedin_url --output emails.csv

Cost & accuracy (May 2026):
    • $10 per 1000 LinkedIn profiles (harvestapi pricing)
    • Hit rate: ~50% on student populations, ~70%+ on working professionals
    • Pay-per-result — failed lookups never bill
    • Verified vs catch-all flags returned per email
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from typing import Sequence

import httpx
import pandas as pd

ACTOR_ID = "harvestapi/linkedin-profile-scraper"
APIFY_BASE = "https://api.apify.com/v2"
DEFAULT_CHUNK_SIZE = 50      # bake-off proved ~50 fits inside 30-min actor timeout
DEFAULT_PARALLEL = 5         # Apify allows up to 25 concurrent runs
DEFAULT_TIMEOUT_S = 1800     # max actor runtime per Apify
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def find_email(
    linkedin_url: str,
    *,
    apify_token: str | None = None,
    actor_timeout_s: int = DEFAULT_TIMEOUT_S,
) -> str | None:
    """Look up the best email for a single LinkedIn profile URL.

    Returns the email string, or None if not found.
    """
    df = find_emails(
        [linkedin_url],
        apify_token=apify_token,
        chunk_size=1,
        parallel=1,
        actor_timeout_s=actor_timeout_s,
        progress=False,
    )
    if df.empty:
        return None
    email = df.iloc[0]["email"]
    return email or None


def find_emails(
    linkedin_urls: Sequence[str],
    *,
    apify_token: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    parallel: int = DEFAULT_PARALLEL,
    actor_timeout_s: int = DEFAULT_TIMEOUT_S,
    progress: bool = True,
) -> pd.DataFrame:
    """Look up emails for a batch of LinkedIn profile URLs.

    Returns a DataFrame with one row per input URL (in original order). Columns:
        linkedin_url, email, all_emails, email_status, email_quality,
        name, headline, company, raw

    `email` is the best candidate (status='valid' wins, else first returned).
    `all_emails` is semicolon-separated.
    `email_status` is one of {valid, risky, invalid, ''}; 'risky' often means
        catch-all domain (mailbox unconfirmed).
    `raw` is the JSON-encoded actor response item, in case you need extra data
        (full work history, skills, etc).
    """
    token = apify_token or os.environ.get("APIFY_TOKEN")
    if not token:
        raise ValueError(
            "Apify token required. Pass apify_token=... or set APIFY_TOKEN env var."
        )

    urls = _normalize_urls(linkedin_urls)
    return asyncio.run(
        _find_emails_async(urls, token, chunk_size, parallel, actor_timeout_s, progress)
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
    urls: list[str], token: str, chunk_size: int, parallel: int,
    actor_timeout_s: int, progress: bool,
) -> pd.DataFrame:
    if not urls:
        return _empty_frame()

    chunks = [urls[i : i + chunk_size] for i in range(0, len(urls), chunk_size)]
    if progress:
        print(f"find_emails: {len(urls)} URLs in {len(chunks)} chunks "
              f"(<= {chunk_size} each), parallel={parallel}")

    sem = asyncio.Semaphore(parallel)
    results_by_slug: dict[str, dict] = {}
    t_start = time.time()

    async with httpx.AsyncClient(timeout=actor_timeout_s + 30) as client:

        async def process_chunk(idx: int, chunk: list[str]) -> None:
            async with sem:
                t0 = time.time()
                try:
                    items = await _call_actor(client, chunk, token, actor_timeout_s)
                except Exception as e:
                    if progress:
                        print(f"  chunk {idx + 1}/{len(chunks)}: ERROR {type(e).__name__}: {str(e)[:120]}")
                    return
                for item in items:
                    slug = _profile_slug(item)
                    if slug:
                        results_by_slug[slug] = item
                if progress:
                    n_with_email = sum(1 for it in items if _extract_emails(it))
                    print(f"  chunk {idx + 1}/{len(chunks)}: "
                          f"{len(items)} returned, {n_with_email} with email "
                          f"({time.time() - t0:.0f}s)")

        await asyncio.gather(*[process_chunk(i, c) for i, c in enumerate(chunks)])

    if progress:
        print(f"find_emails: done in {time.time() - t_start:.0f}s")

    return _build_dataframe(urls, results_by_slug)


async def _call_actor(
    client: httpx.AsyncClient, urls: list[str], token: str, timeout_s: int,
) -> list[dict]:
    """Run the harvestapi actor synchronously: start → poll → fetch dataset."""
    actor_path = ACTOR_ID.replace("/", "~")
    payload = {
        "profileScraperMode": "Profile details + email search ($10 per 1k)",
        "queries": urls,
    }

    # Start
    r = await client.post(
        f"{APIFY_BASE}/acts/{actor_path}/runs",
        params={"token": token},
        json=payload,
    )
    r.raise_for_status()
    run = r.json().get("data", {})
    run_id = run["id"]

    # Poll
    deadline = time.time() + timeout_s
    status = run.get("status")
    while status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
        if time.time() > deadline:
            return []
        await asyncio.sleep(5)
        r = await client.get(
            f"{APIFY_BASE}/actor-runs/{run_id}", params={"token": token}
        )
        run = r.json().get("data", {})
        status = run.get("status")

    if status != "SUCCEEDED":
        return []

    # Fetch dataset
    ds_id = run.get("defaultDatasetId")
    if not ds_id:
        return []
    r = await client.get(
        f"{APIFY_BASE}/datasets/{ds_id}/items",
        params={"token": token, "format": "json", "clean": "true"},
    )
    r.raise_for_status()
    return r.json() or []


def _profile_slug(item: dict) -> str:
    """Extract the LinkedIn profile slug (the part after /in/) for matching.

    Matches by slug rather than full URL because actors return URLs in their
    own canonical form (e.g. https://www.linkedin.com/in/<slug>) while inputs
    may use https://linkedin.com/in/<slug>.
    """
    pid = item.get("publicIdentifier")
    if isinstance(pid, str) and pid.strip():
        return pid.strip().lower()
    for key in ("linkedinUrl", "url", "profileUrl", "input_url", "profile_url"):
        v = item.get(key)
        if isinstance(v, str) and "linkedin.com/in/" in v.lower():
            return _slug_from_url(v)
    blob = json.dumps(item, default=str)
    m = re.search(r"linkedin\.com/in/([^/?#\"' ]+)", blob, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _slug_from_url(url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url.lower())
    return m.group(1) if m else ""


def _extract_emails(item: dict) -> list[dict]:
    """Pull harvestapi's `emails` field. Returns list of dicts, status='valid'
    sorted first. Each dict: {email, status, qualityScore, catchAllDomain, ...}."""
    raw = item.get("emails") or []
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if isinstance(entry, dict) and entry.get("email"):
            out.append(entry)
        elif isinstance(entry, str) and "@" in entry:
            out.append({"email": entry})
    # Rank: status=valid > risky > anything else; high quality first
    rank = {"valid": 0, "risky": 1}
    out.sort(key=lambda e: (
        rank.get((e.get("status") or "").lower(), 9),
        -(e.get("qualityScore") or 0),
    ))
    return out


def _extract_company(item: dict) -> str:
    pos = item.get("currentPosition") or item.get("position")
    if isinstance(pos, dict):
        return pos.get("companyName") or pos.get("company") or ""
    if isinstance(pos, list) and pos and isinstance(pos[0], dict):
        return pos[0].get("companyName") or pos[0].get("company") or ""
    return item.get("companyName") or ""


def _full_name(item: dict) -> str:
    if item.get("name"):
        return str(item["name"])
    parts = [item.get("firstName") or "", item.get("lastName") or ""]
    return " ".join(p for p in parts if p).strip()


def _build_dataframe(urls: list[str], results_by_slug: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for url in urls:
        slug = _slug_from_url(url)
        item = results_by_slug.get(slug, {})
        emails = _extract_emails(item)
        best = emails[0] if emails else {}
        rows.append({
            "linkedin_url": url,
            "email": best.get("email", "") or "",
            "all_emails": "; ".join(e.get("email", "") for e in emails),
            "email_status": best.get("status", "") or "",
            "email_quality": best.get("qualityScore", "") or "",
            "name": _full_name(item),
            "headline": item.get("headline", "") or "",
            "company": _extract_company(item),
            "raw": json.dumps(item, default=str) if item else "",
        })
    return pd.DataFrame(rows)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "linkedin_url", "email", "all_emails", "email_status",
        "email_quality", "name", "headline", "company", "raw",
    ])


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Find emails for LinkedIn profile URLs via Apify harvestapi. "
            "Reads a CSV, writes a CSV with the email columns appended."
        ),
    )
    ap.add_argument("input_csv", help="Path to input CSV file")
    ap.add_argument("--url-column", default="linkedin_url",
                    help="Column name containing LinkedIn URLs (default: linkedin_url)")
    ap.add_argument("--output", default="emails.csv",
                    help="Output CSV path (default: emails.csv)")
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    ap.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL)
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N rows (for dry runs)")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except ImportError:
        pass  # dotenv optional

    df_in = pd.read_csv(args.input_csv)
    if args.url_column not in df_in.columns:
        print(f"ERROR: column '{args.url_column}' not in input CSV.", file=sys.stderr)
        print(f"Available columns: {list(df_in.columns)}", file=sys.stderr)
        return 2

    if args.limit:
        df_in = df_in.head(args.limit)

    urls = df_in[args.url_column].dropna().astype(str).tolist()
    print(f"Reading {len(urls)} URLs from {args.input_csv}")

    df = find_emails(
        urls,
        chunk_size=args.chunk_size,
        parallel=args.parallel,
    )

    # Merge back original columns alongside the email results
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
