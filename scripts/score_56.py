"""Score b2b_leads + harvestapi outputs against ground truth.

Reads the two Apify datasets directly via the API (so we don't depend on the
broken in-pipeline orchestrator path), joins by LinkedIn public identifier,
and emits per-person + per-provider metrics.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pandas as pd


def public_id(url: str) -> str:
    u = (url or "").strip().rstrip("/").lower()
    if "/in/" in u:
        u = u.split("/in/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    return u


def fetch_dataset(token: str, dataset_id: str) -> list[dict]:
    r = httpx.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        params={"token": token, "clean": "true"},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def b2b_emails(item: dict) -> list[dict]:
    """Return list of {email, status, score, source} for a b2b_leads item."""
    out = []
    raw = item.get("emails") or []
    for e in raw:
        if isinstance(e, dict):
            out.append({"email": (e.get("email") or "").lower(), **{k: e.get(k) for k in ("status", "qualityScore", "deliverable")}})
        elif isinstance(e, str) and "@" in e:
            out.append({"email": e.lower()})
    best = item.get("best_email")
    if best and not any(o["email"] == best.lower() for o in out):
        out.append({"email": best.lower(), "status": "best_email_field"})
    return [o for o in out if o.get("email")]


def harv_emails(item: dict) -> list[dict]:
    out = []
    raw = item.get("emails") or []
    for e in raw:
        if isinstance(e, dict):
            out.append({"email": (e.get("email") or "").lower(), **{k: e.get(k) for k in ("status", "qualityScore", "deliverable")}})
        elif isinstance(e, str) and "@" in e:
            out.append({"email": e.lower()})
    return [o for o in out if o.get("email")]


def main():
    token = open(".env").read().split("APIFY_TOKEN=")[1].split()[0].strip()
    b2b_dataset = sys.argv[1] if len(sys.argv) > 1 else "a5wOCDDVGgEdi2aYu"
    harv_dataset = sys.argv[2] if len(sys.argv) > 2 else "7NlEBBT4dR62IQyhV"

    print(f"b2b_leads dataset:  {b2b_dataset}")
    print(f"harvestapi dataset: {harv_dataset}")

    b2b_items = fetch_dataset(token, b2b_dataset)
    harv_items = fetch_dataset(token, harv_dataset)
    print(f"b2b_leads items:    {len(b2b_items)}")
    print(f"harvestapi items:   {len(harv_items)}")

    b2b_by_id = {public_id(it.get("linkedin_url") or it.get("public_identifier") or ""): it for it in b2b_items}
    harv_by_id = {public_id(it.get("linkedinUrl") or it.get("publicIdentifier") or ""): it for it in harv_items}

    df = pd.read_csv("data/ground_truth/ai_fund_edu_students.tsv", sep="\t", dtype=str).fillna("")
    rows = []
    for _, p in df.iterrows():
        pid = public_id(p["linkedin_url"])
        truth = p["edu_email"].lower()
        truth_domain = truth.split("@", 1)[1] if "@" in truth else ""

        b2b = b2b_by_id.get(pid)
        harv = harv_by_id.get(pid)
        b2b_e = b2b_emails(b2b) if b2b else []
        harv_e = harv_emails(harv) if harv else []

        def pick(emails, prefer_domain):
            if not emails:
                return None
            if prefer_domain:
                for e in emails:
                    if e["email"].endswith("@" + prefer_domain):
                        return e
            for e in emails:
                if e.get("status") == "valid" or e.get("deliverable"):
                    return e
            return emails[0]

        b2b_pick = pick(b2b_e, truth_domain)
        harv_pick = pick(harv_e, truth_domain)

        rows.append({
            "name": p["name"],
            "linkedin_url": p["linkedin_url"],
            "truth_email": truth,
            "b2b_email": (b2b_pick or {}).get("email", ""),
            "b2b_status": (b2b_pick or {}).get("status", ""),
            "b2b_score": (b2b_pick or {}).get("qualityScore", ""),
            "b2b_all": "|".join(e["email"] for e in b2b_e),
            "harv_email": (harv_pick or {}).get("email", ""),
            "harv_status": (harv_pick or {}).get("status", ""),
            "harv_score": (harv_pick or {}).get("qualityScore", ""),
            "harv_all": "|".join(e["email"] for e in harv_e),
            "b2b_exact": int(any(e["email"] == truth for e in b2b_e)),
            "harv_exact": int(any(e["email"] == truth for e in harv_e)),
            "b2b_domain": int(any(e["email"].endswith("@" + truth_domain) for e in b2b_e) and truth_domain),
            "harv_domain": int(any(e["email"].endswith("@" + truth_domain) for e in harv_e) and truth_domain),
            "b2b_any_email": int(bool(b2b_e)),
            "harv_any_email": int(bool(harv_e)),
        })

    out = pd.DataFrame(rows)
    out_path = Path("data/output/full56_scored.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    print(f"\nWrote {out_path}")

    n = len(out)
    print(f"\n=== Summary on {n} students ===")
    print(f"Coverage (any email returned)")
    print(f"  b2b_leads:  {out['b2b_any_email'].sum():2d} / {n}  ({out['b2b_any_email'].mean():.0%})")
    print(f"  harvestapi: {out['harv_any_email'].sum():2d} / {n}  ({out['harv_any_email'].mean():.0%})")
    print(f"\nExact match against truth_email")
    print(f"  b2b_leads:  {out['b2b_exact'].sum():2d} / {n}  ({out['b2b_exact'].mean():.0%})")
    print(f"  harvestapi: {out['harv_exact'].sum():2d} / {n}  ({out['harv_exact'].mean():.0%})")
    print(f"\nSchool-domain hit (right university, any local-part)")
    print(f"  b2b_leads:  {out['b2b_domain'].sum():2d} / {n}  ({out['b2b_domain'].mean():.0%})")
    print(f"  harvestapi: {out['harv_domain'].sum():2d} / {n}  ({out['harv_domain'].mean():.0%})")
    print(f"\nUnion (either provider exact OR school-domain hit)")
    union_exact = ((out['b2b_exact'] | out['harv_exact']) > 0).sum()
    union_domain = ((out['b2b_domain'] | out['harv_domain']) > 0).sum()
    print(f"  exact:  {union_exact:2d} / {n}  ({union_exact/n:.0%})")
    print(f"  domain: {union_domain:2d} / {n}  ({union_domain/n:.0%})")


if __name__ == "__main__":
    main()
