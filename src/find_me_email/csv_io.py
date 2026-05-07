"""Flexible CSV reader. Maps arbitrary column headers onto the Person schema."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from find_me_email.college_domains import resolve_domain
from find_me_email.schemas import EmailCandidate, EnrichmentResult, Person

# Column header aliases (case-insensitive, stripped of non-alphanumerics).
ALIASES: dict[str, list[str]] = {
    "name": ["name", "fullname", "full_name", "personname"],
    "first_name": ["first", "firstname", "first_name", "givenname"],
    "last_name": ["last", "lastname", "last_name", "familyname", "surname"],
    "linkedin_url": ["linkedin", "linkedinurl", "linkedin_url", "profile", "profileurl", "profile_url", "url"],
    "company": ["company", "employer", "organization", "org"],
    "school": ["school", "college", "university", "institution", "alma_mater", "education"],
    "school_domain": ["domain", "schooldomain", "edudomain", "emaildomain"],
    "title": ["title", "role", "position", "jobtitle"],
    "location": ["location", "city", "geo", "region"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _column_map(headers: list[str]) -> dict[str, str]:
    """Map source CSV columns → Person field names."""
    norm_headers = {_norm(h): h for h in headers}
    out: dict[str, str] = {}
    for field, aliases in ALIASES.items():
        for alias in aliases:
            if alias in norm_headers and field not in out:
                out[field] = norm_headers[alias]
                break
    return out


def read_people(path: Path, sample: int | None = None, seed: int = 42) -> list[Person]:
    df = pd.read_csv(path)
    cmap = _column_map(list(df.columns))
    if sample and len(df) > sample:
        df = df.sample(n=sample, random_state=seed).reset_index(drop=True)

    people: list[Person] = []
    for idx, row in df.iterrows():
        kwargs: dict[str, Any] = {"row_id": str(row.get(cmap.get("row_id"), idx))}
        for field, src_col in cmap.items():
            val = row.get(src_col)
            if pd.isna(val) or val == "":
                continue
            kwargs[field] = str(val).strip()
        # Auto-resolve school_domain if school is present but domain isn't.
        if kwargs.get("school") and not kwargs.get("school_domain"):
            dom = resolve_domain(kwargs["school"])
            if dom:
                kwargs["school_domain"] = dom
        # Stash unmapped columns in extra so nothing is lost.
        extra = {c: row[c] for c in df.columns if c not in cmap.values() and not pd.isna(row[c])}
        if extra:
            kwargs["extra"] = {k: (str(v) if not isinstance(v, (int, float, bool)) else v) for k, v in extra.items()}
        people.append(Person(**kwargs))
    return people


def write_results(results: list[EnrichmentResult], path: Path) -> None:
    rows = []
    for r in results:
        best = r.best
        base = {
            "row_id": r.person.row_id,
            "name": r.person.name or f"{r.person.first_name or ''} {r.person.last_name or ''}".strip(),
            "linkedin_url": str(r.person.linkedin_url) if r.person.linkedin_url else "",
            "school": r.person.school or "",
            "school_domain": r.person.school_domain or "",
            "best_email": best.email if best else "",
            "best_confidence": best.confidence.value if best else "",
            "best_source": best.source_provider if best else "",
            "best_verified": best.verified if best else False,
            "best_notes": best.notes if best else "no candidates returned",
            "all_candidates": "; ".join(f"{c.email} [{c.confidence.value}/{c.source_provider}]" for c in r.candidates),
            "providers_attempted": ",".join(r.providers_attempted),
            "total_cost_usd": round(r.total_cost_usd, 6),
        }
        rows.append(base)
    pd.DataFrame(rows).to_csv(path, index=False)
