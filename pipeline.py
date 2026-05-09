"""End-to-end pipeline: ground-truth benchmark for the bhansali recreation.

For each row in test_20.csv (20 students, all with primary_email):
  1. Run a per-name DDG query with stealth+warmup
  2. Collect every (slug, email) hit
  3. Score each pair via confidence.py
  4. Pick the best hit whose slug matches this student's LinkedIn slug
  5. Compare returned email vs ground-truth email
  6. Report precision / recall / confidence-bucketed accuracy

Run:
    python pipeline.py --input data/input/test_20.csv --limit 20

Outputs:
    data/output/pipeline_test_20.csv  (per-row results)
    Stdout: aggregate metrics
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
from playwright_stealth import Stealth
from playwright.sync_api import sync_playwright

from browser_scraper import (
    Hit, RunResult,
    build_query, scrape_duckduckgo,
    make_browser, make_context, maybe_dismiss_consent,
    dedupe, jitter,
)
from confidence import score as score_pair, _slug_clean


SCHOOL_DOMAINS = {
    "University of California, Berkeley": "@berkeley.edu",
    "Stanford University": "@stanford.edu",
    "San Jose State University": "@sjsu.edu",
    "Santa Clara University": "@scu.edu",
    "San Francisco State University": "@sfsu.edu",
    "University of San Francisco": "@usfca.edu",
    "Carnegie Mellon University": "@andrew.cmu.edu",
    "De Anza College": "@deanza.edu",
}


def slug_from_url(url: str) -> str:
    m = re.search(r"linkedin\.com/(?:in|pub)/([a-zA-Z0-9\-_%]+)", (url or "").lower())
    return m.group(1) if m else ""


def build_name_queries(full_name: str, school: str) -> list[tuple[str, str]]:
    """Returns [(label, query), ...] in the order to try them.

    First we try plain `site:linkedin.com/ "<full_name>"` (no domain pin).
    If that returns no emails, the runner can fall back to domain-pinned queries.
    """
    name_q = build_query(keyword=full_name, location="", email_domain="")
    out = [("name_only", name_q)]

    domain = SCHOOL_DOMAINS.get(school, "")
    if domain:
        out.append((
            f"name+{domain}",
            build_query(keyword=full_name, location="", email_domain=domain),
        ))
    out.append((
        "name+@gmail.com",
        build_query(keyword=full_name, location="", email_domain="@gmail.com"),
    ))
    return out


def run_one(page, full_name: str, school: str, *, max_pages: int) -> tuple[list[Hit], str]:
    """Run queries until we get hits with emails, return (hits, query_used)."""
    queries = build_name_queries(full_name, school)
    last_used = ""
    accumulated: list[Hit] = []
    for label, q in queries:
        last_used = label
        res = RunResult(backend="duckduckgo", query=q)
        try:
            scrape_duckduckgo(page, q, max_pages=max_pages, snap_dir=None, res=res)
        except Exception as e:
            print(f"    !! {label}: {type(e).__name__}: {str(e)[:120]}")
            continue
        accumulated.extend(res.hits)
        if any(h.email for h in res.hits):
            break  # got at least one email, stop trying alternates
        # else: try next query variant
        jitter(2, 4)
    return dedupe(accumulated), last_used


def best_hit_for(person_slug: str, hits: list[Hit], full_name: str) -> tuple[Hit | None, dict]:
    """Pick the highest-confidence hit whose slug matches the person.

    Slug match is fuzzy: exact, prefix, or first-N chars (handles trailing
    LinkedIn id suffixes).
    """
    p_clean = _slug_clean(person_slug)
    candidates: list[tuple[float, Hit, dict]] = []
    for h in hits:
        if not h.email:
            continue
        h_clean = _slug_clean(h.linkedin_slug)
        # Slug must match the person we're looking up, not someone else
        if h_clean != p_clean and not (
            h_clean.startswith(p_clean) or p_clean.startswith(h_clean)
        ):
            continue
        sc = score_pair(full_name, h.linkedin_slug, h.email)
        candidates.append((sc["composite"], h, sc))
    if not candidates:
        return None, {"composite": 0.0, "bucket": "NONE"}
    candidates.sort(key=lambda x: -x[0])
    _, best, sc = candidates[0]
    return best, sc


def run_pipeline(input_csv: Path, limit: int, out_csv: Path, max_pages: int = 2) -> None:
    df = pd.read_csv(input_csv)
    if limit:
        df = df.head(limit)

    rows = []
    print(f"Pipeline on {len(df)} rows from {input_csv}")
    print("-" * 78)
    print(f"{'#':>2} {'name':22s} {'found_email':32s} {'match':5s} {'conf':5s} {'bucket':5s}")
    print("-" * 78)

    with Stealth().use_sync(sync_playwright()) as p:
        browser = make_browser(p, headless=True)
        ctx = make_context(browser)
        page = ctx.new_page()

        # one-time warmup
        page.goto("https://duckduckgo.com/", wait_until="domcontentloaded",
                  timeout=20000)
        maybe_dismiss_consent(page)
        jitter(2, 3)

        for i, row in df.reset_index(drop=True).iterrows():
            name = str(row["full_name"])
            school = str(row.get("school_name", ""))
            li_url = str(row.get("linkedin_url", ""))
            gt_email = str(row.get("primary_email") or "").strip().lower()
            person_slug = slug_from_url(li_url)

            t0 = time.time()
            hits, q_used = run_one(page, name, school, max_pages=max_pages)
            elapsed = time.time() - t0

            best, sc = best_hit_for(person_slug, hits, name)
            found_email = best.email.lower() if best else ""
            match = "exact" if found_email and found_email == gt_email else (
                    "domain-only" if (found_email and gt_email
                                       and found_email.split("@")[-1] == gt_email.split("@")[-1])
                    else ("found" if found_email else ""))

            # Also surface emails returned but for *other* people sharing the name
            other_emails = [h.email for h in hits
                            if h.email and h.email.lower() != found_email
                            and _slug_clean(h.linkedin_slug) != _slug_clean(person_slug)]

            row_out = {
                "person_id": row.get("person_id"),
                "full_name": name,
                "school": school,
                "person_slug": person_slug,
                "gt_email": gt_email,
                "found_email": found_email,
                "match": match,
                "confidence": sc["composite"],
                "bucket": sc["bucket"],
                "query_used": q_used,
                "n_hits_total": len(hits),
                "n_hits_with_email": sum(1 for h in hits if h.email),
                "n_other_people_emails": len(other_emails),
                "other_emails_sample": ";".join(other_emails[:3]),
                "elapsed_s": round(elapsed, 1),
            }
            rows.append(row_out)
            print(f"{i+1:>2} {name[:22]:22s} {(found_email or '—')[:32]:32s} "
                  f"{match[:5]:5s} {sc['composite']:5.2f} {sc['bucket']:5s}")

        ctx.close()
        browser.close()

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df = pd.DataFrame(rows)
    out_df.to_csv(out_csv, index=False)

    # ─── Aggregate metrics ───
    n = len(out_df)
    n_returned = (out_df["found_email"] != "").sum()
    n_exact = (out_df["match"] == "exact").sum()
    n_domain_only = (out_df["match"] == "domain-only").sum()
    n_high = (out_df["bucket"] == "HIGH").sum()
    n_high_correct = ((out_df["bucket"] == "HIGH") & (out_df["match"] == "exact")).sum()
    n_med = (out_df["bucket"] == "MED").sum()
    n_med_correct = ((out_df["bucket"] == "MED") & (out_df["match"] == "exact")).sum()

    print("\n" + "=" * 78)
    print("AGGREGATE METRICS")
    print("=" * 78)
    print(f"  Rows processed                : {n}")
    print(f"  Returned an email (any conf.) : {n_returned}/{n} = {pct(n_returned, n)}")
    print(f"  Exact match to ground truth   : {n_exact}/{n}  ({pct(n_exact, n)} recall)")
    print(f"  Same-domain (wrong local-part): {n_domain_only}/{n}")
    print()
    print("  By confidence bucket:")
    print(f"    HIGH returned : {n_high}, of which exact match: {n_high_correct} "
          f"= {pct(n_high_correct, n_high)} precision")
    print(f"    MED  returned : {n_med}, of which exact match: {n_med_correct} "
          f"= {pct(n_med_correct, n_med)} precision")
    print()
    print(f"  Wrote {out_csv}")


def pct(num: int, denom: int) -> str:
    if denom == 0:
        return "n/a"
    return f"{100 * num / denom:.0f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/input/test_20.csv",
                    help="CSV with full_name, linkedin_url, primary_email columns")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default="data/output/pipeline_test_20.csv")
    ap.add_argument("--max-pages", type=int, default=2)
    args = ap.parse_args()

    inp = Path(args.input)
    if not inp.is_absolute():
        # Allow relative paths from project root or worktree
        if not inp.exists():
            project_root = Path(__file__).resolve().parents[3]  # worktree → .claude → root
            alt = project_root / inp
            if alt.exists():
                inp = alt
    if not inp.exists():
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        return 2

    out = Path(args.out)
    run_pipeline(inp, args.limit, out, max_pages=args.max_pages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
