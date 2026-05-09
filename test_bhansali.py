"""Thesis test for `bhansalisoft/linkedin-email-scraper`.

Goal: determine whether this actor is useful for find-me-email's use case
(looking up emails for *known LinkedIn profile URLs*).

Spoiler from the input schema: this actor does NOT take URLs. It takes
keyword + location + country and runs Google searches. So it is structurally
unsuited as a drop-in replacement. This harness empirically verifies that and
also tests whether it could work as a name-based fallback.

Run:
    # Dry run (no API call, just shows the input payload)
    python test_bhansali.py --dry-run --name "Satya Nadella"

    # Live, single small search (uses subscription credits)
    python test_bhansali.py --live --name "Satya Nadella" --limit 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ACTOR_PATH = "bhansalisoft~linkedin-email-scraper"
APIFY_BASE = "https://api.apify.com/v2"
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Country code map (Apify's enum from the actor's input schema).
# "www" = United States. Pick what most matches your audience.
COUNTRY_US = "www"


def build_input(
    *, keyword: str, location: str = "", country: str = COUNTRY_US,
    email_type: str = "0", other_email: str = "", limit: str = "5",
) -> dict:
    """Build the actor input payload from the actor's input schema."""
    payload = {
        "Keyword": keyword,
        "location": location,
        "social_network": "linkedin.com/",
        "Country": country,
        "Email_Type": email_type,
        "Other_Email_Type": other_email,
        "Limit": str(limit),
        "proxySettings": {"useApifyProxy": False},
    }
    return payload


def run_actor_sync(token: str, payload: dict, timeout_s: int = 600) -> dict:
    """Start, poll, and fetch dataset. Returns {status, run_id, items, raw}."""
    out: dict = {"status": "UNSTARTED", "items": []}

    with httpx.Client(timeout=timeout_s + 30) as client:
        # Start
        r = client.post(
            f"{APIFY_BASE}/acts/{ACTOR_PATH}/runs",
            params={"token": token},
            json=payload,
        )
        if r.status_code >= 400:
            out["error"] = f"start failed: {r.status_code} {r.text[:300]}"
            return out
        run = r.json().get("data", {})
        run_id = run.get("id")
        out["run_id"] = run_id
        print(f"  started run {run_id}, polling...")

        # Poll
        deadline = time.time() + timeout_s
        status = run.get("status")
        while status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            if time.time() > deadline:
                out["status"] = "CLIENT_TIMEOUT"
                return out
            time.sleep(5)
            r = client.get(
                f"{APIFY_BASE}/actor-runs/{run_id}", params={"token": token}
            )
            run = r.json().get("data", {})
            status = run.get("status")
            elapsed = int(time.time() - (deadline - timeout_s))
            print(f"  [{elapsed:>3}s] status={status}")

        out["status"] = status

        if status != "SUCCEEDED":
            out["error"] = f"run ended with status={status}"
            return out

        # Fetch dataset
        ds_id = run.get("defaultDatasetId")
        if not ds_id:
            out["error"] = "no dataset id in run result"
            return out
        r = client.get(
            f"{APIFY_BASE}/datasets/{ds_id}/items",
            params={"token": token, "format": "json", "clean": "true"},
        )
        if r.status_code >= 400:
            out["error"] = f"dataset fetch failed: {r.status_code}"
            return out
        out["items"] = r.json() or []
        return out


def emails_in(items: list[dict]) -> list[str]:
    """Extract every email-looking string from the actor's dataset items."""
    seen = set()
    for it in items:
        for v in EMAIL_RX.findall(json.dumps(it)):
            seen.add(v.lower())
    return sorted(seen)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="Satya Nadella",
                    help="Name or keyword to search for")
    ap.add_argument("--location", default="")
    ap.add_argument("--country", default=COUNTRY_US,
                    help="Apify country code (www=US, in=India, uk=UK, ca=Canada)")
    ap.add_argument("--email-type", default="0", choices=["0", "1"],
                    help="0 = popular (gmail/yahoo/hotmail), 1 = custom domain")
    ap.add_argument("--other-email", default="",
                    help="Custom domain like @stanford.edu (used when --email-type=1)")
    ap.add_argument("--limit", default="5",
                    help="Max emails to scrape (0 = unlimited)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build the payload, don't call the API")
    ap.add_argument("--live", action="store_true",
                    help="Actually invoke the actor (uses paid credits)")
    ap.add_argument("--out", default="data/output/bhansali_thesis_run.json",
                    help="Where to write the raw response")
    args = ap.parse_args()

    if not args.dry_run and not args.live:
        print("Pick one of --dry-run or --live", file=sys.stderr)
        return 2

    payload = build_input(
        keyword=args.name, location=args.location, country=args.country,
        email_type=args.email_type, other_email=args.other_email,
        limit=args.limit,
    )

    print("=" * 70)
    print(f"Actor:    {ACTOR_PATH}")
    print(f"Mode:     {'LIVE (paid)' if args.live else 'DRY-RUN'}")
    print("Payload:")
    print(json.dumps(payload, indent=2))
    print("=" * 70)

    if args.dry_run:
        print("\nDry-run only. Add --live to actually call the actor.")
        return 0

    load_dotenv()
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print("ERROR: APIFY_TOKEN not set", file=sys.stderr)
        return 2

    t0 = time.time()
    result = run_actor_sync(token, payload)
    elapsed = time.time() - t0

    print(f"\nFinished in {elapsed:.0f}s, status={result['status']}")
    if "error" in result:
        print(f"ERROR: {result['error']}")
    items = result.get("items", [])
    emails = emails_in(items)
    print(f"Items returned: {len(items)}")
    print(f"Unique emails:  {len(emails)}")
    for e in emails[:20]:
        print(f"  • {e}")
    if len(emails) > 20:
        print(f"  ... and {len(emails) - 20} more")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "input": payload,
        "result_status": result["status"],
        "elapsed_s": round(elapsed, 1),
        "n_items": len(items),
        "n_emails": len(emails),
        "emails_sample": emails[:50],
        "items_sample": items[:5],
    }, indent=2, default=str))
    print(f"\nWrote {out_path}")

    return 0 if result["status"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    sys.exit(main())
