"""Bake-off: run N candidate Apify actors against a test CSV with ground truth.

Standalone — no project dependency. Only uses httpx + pandas + python-dotenv.
Edit ACTORS below to compare different actors. The TEST_CSV must have columns
`linkedin_url` and `primary_email` (the ground-truth email).

Run:
    python scripts/actor_bakeoff.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["APIFY_TOKEN"]
APIFY_BASE = "https://api.apify.com/v2"

TEST_CSV = Path("data/input/test_20.csv")
RESULTS_DIR = Path("data/output/bakeoff")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def b_harvest(urls):
    return {
        "profileScraperMode": "Profile details + email search ($10 per 1k)",
        "queries": urls,
    }


def b_anchor(urls):
    return {"startUrls": [{"url": u} for u in urls]}


def b_vulnv(urls):
    return {"urls": urls}


def b_supreme(urls):
    return {"profileUrls": urls}


def b_b2b_leads(urls):
    return {"queries": urls}


def b_bebity(urls):
    return {"urls": urls, "type": "profile"}


# 5 actors. Replaced earlier failures with viable alternates.
ACTORS = [
    ("harvestapi/linkedin-profile-scraper", b_harvest),
    ("anchor/linkedin-to-email", b_anchor),
    ("vulnv/linkedin-email-finder", b_vulnv),
    ("supreme_coder/linkedin-profile-scraper", b_supreme),
    ("b2b_leads/linkedin-profile-scraper", b_b2b_leads),
]


def emails_from_record(rec) -> list[str]:
    out = set()
    for m in EMAIL_RX.findall(json.dumps(rec)):
        out.add(m.lower())
    return sorted(out)


def linkedin_id(url: str) -> str:
    m = re.search(r"/in/([^/?#]+)", (url or "").lower())
    return m.group(1) if m else (url or "").lower().rstrip("/")


def find_url_in_record(rec) -> str:
    """Look for a LinkedIn URL anywhere in the record."""
    for k in ("linkedinUrl", "url", "profileUrl", "linkedin_url",
             "input_url", "inputUrl", "publicIdentifier", "profile_url"):
        v = rec.get(k)
        if isinstance(v, str) and "linkedin.com" in v.lower():
            return v
    blob = json.dumps(rec)
    m = re.search(r'https?://[^\s"]*linkedin\.com/in/[^\s"]+', blob)
    return m.group(0) if m else ""


async def call_actor(actor_id: str, payload: dict, timeout_s: int = 900) -> tuple:
    """Submit a synchronous Apify actor run, poll until done, return dataset."""
    t0 = time.time()
    actor_path = actor_id.replace("/", "~")
    try:
        async with httpx.AsyncClient(timeout=timeout_s + 30) as client:
            r = await client.post(
                f"{APIFY_BASE}/acts/{actor_path}/runs",
                params={"token": TOKEN}, json=payload,
            )
            r.raise_for_status()
            run = r.json().get("data", {})
            run_id = run["id"]
            deadline = time.time() + timeout_s
            status = run.get("status")
            while status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                if time.time() > deadline:
                    return [], {"elapsed_s": time.time() - t0, "ok": False,
                                "error": "timeout"}
                await asyncio.sleep(5)
                r = await client.get(f"{APIFY_BASE}/actor-runs/{run_id}",
                                     params={"token": TOKEN})
                run = r.json().get("data", {})
                status = run.get("status")
            if status != "SUCCEEDED":
                return [], {"elapsed_s": time.time() - t0, "ok": False,
                            "error": f"run_status={status}"}
            ds_id = run.get("defaultDatasetId")
            r = await client.get(
                f"{APIFY_BASE}/datasets/{ds_id}/items",
                params={"token": TOKEN, "format": "json", "clean": "true"},
            )
            r.raise_for_status()
            items = r.json() or []
        return items, {"elapsed_s": time.time() - t0, "ok": True}
    except Exception as e:
        return [], {"elapsed_s": time.time() - t0, "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:300]}"}


async def run_one(actor_id, input_fn, urls, truth):
    print(f"\n━━━ {actor_id} ━━━")
    items, meta = await call_actor(actor_id, input_fn(urls), timeout_s=900)
    print(f"  ok={meta['ok']} items={len(items)} elapsed={meta['elapsed_s']:.0f}s")
    if not meta["ok"]:
        print(f"  ERROR: {meta.get('error','')[:200]}")
        return {"actor": actor_id, "found": 0, "agree": 0,
                "elapsed_s": meta["elapsed_s"], "error": meta.get("error", "")}

    # Per-URL email extraction
    by_slug: dict[str, list[str]] = {}
    unmatched_emails: list[str] = []
    for item in items:
        url = find_url_in_record(item)
        emails = emails_from_record(item)
        if not emails:
            continue
        if url:
            slug = linkedin_id(url)
            by_slug.setdefault(slug, []).extend(emails)
        else:
            unmatched_emails.extend(emails)

    rows = []
    found = agree = 0
    for url in urls:
        slug = linkedin_id(url)
        emails = sorted(set(by_slug.get(slug, [])))
        true_email = truth.get(url, "").lower()
        ok = bool(true_email) and (true_email in emails)
        rows.append({
            "linkedin_url": url, "true_email": true_email,
            "emails_found": "; ".join(emails),
            "n_emails": len(emails),
            "truth_in_results": ok,
        })
        if emails:
            found += 1
        if ok:
            agree += 1

    safe_id = actor_id.replace("/", "_")
    pd.DataFrame(rows).to_csv(RESULTS_DIR / f"{safe_id}.csv", index=False)
    (RESULTS_DIR / f"{safe_id}_raw.json").write_text(
        json.dumps(items[:30], indent=2, default=str)
    )
    print(f"  rows with email: {found}/{len(urls)}  truth-match: {agree}/{len(urls)}  "
          f"unmatched_emails={len(unmatched_emails)}")
    return {"actor": actor_id, "found": found, "agree": agree,
            "n_test": len(urls), "elapsed_s": meta["elapsed_s"],
            "unmatched_emails": len(unmatched_emails)}


async def main():
    test = pd.read_csv(TEST_CSV)
    raw_urls = test["linkedin_url"].astype(str).tolist()
    urls = [u if u.startswith("http") else f"https://{u}" for u in raw_urls]
    truth = {urls[i]: str(test["primary_email"].iloc[i]).lower().strip()
             for i in range(len(urls))}
    print(f"Test set: {len(urls)} LinkedIn URLs")
    print(f"Truth domains: {sorted(set(t.split('@')[-1] for t in truth.values() if '@' in t))[:8]}...")

    summaries = []
    for actor_id, input_fn in ACTORS:
        try:
            s = await run_one(actor_id, input_fn, urls, truth)
        except Exception as e:
            s = {"actor": actor_id, "error": f"{type(e).__name__}: {e}",
                 "found": 0, "agree": 0, "elapsed_s": 0}
        summaries.append(s)

    print("\n" + "═" * 90)
    print(f"{'Actor':<50} {'found':>6} {'agree':>6} {'time':>8}")
    print("─" * 90)
    for s in summaries:
        t = f"{s.get('elapsed_s', 0):.0f}s"
        print(f"{s['actor']:<50} {s.get('found', 0):>6} {s.get('agree', 0):>6} {t:>8}")
    print("═" * 90)
    pd.DataFrame(summaries).to_csv(RESULTS_DIR / "summary.csv", index=False)


if __name__ == "__main__":
    asyncio.run(main())
