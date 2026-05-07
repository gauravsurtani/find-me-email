"""Flexible CSV/TSV reader. Maps arbitrary column headers onto the Person schema."""
from __future__ import annotations

import csv
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
    "school": ["school", "college", "university", "institution", "alma_mater", "education", "schoolname"],
    "school_domain": ["domain", "schooldomain", "edudomain", "emaildomain"],
    "title": ["title", "role", "position", "jobtitle", "headline", "linkedinheadline"],
    "location": ["location", "city", "geo", "region", "locationdisplay"],
}

# Truth-column candidates, in priority order. The first one found is used.
TRUTH_PRIORITY = ["edu_email", "primary_email", "email", "all_emails"]


def _sniff_delimiter(path: Path) -> str:
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        sample = f.read(8192)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t|;").delimiter
    except csv.Error:
        return "\t" if sample.count("\t") > sample.count(",") else ","


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


def _read_df(path: Path) -> pd.DataFrame:
    sep = _sniff_delimiter(path)
    df = pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False, na_values=[""], engine="python")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _row_id_column(df: pd.DataFrame) -> str | None:
    norm_to_orig = {_norm(c): c for c in df.columns}
    for cand in ("row_id", "person_id", "id"):
        nc = _norm(cand)
        if nc in norm_to_orig:
            return norm_to_orig[nc]
    return None


def _truth_column(df: pd.DataFrame) -> str | None:
    norm_to_orig = {_norm(c): c for c in df.columns}
    for cand in TRUTH_PRIORITY:
        nc = _norm(cand)
        if nc in norm_to_orig:
            return norm_to_orig[nc]
    return None


def read_people(path: Path, sample: int | None = None, seed: int = 42) -> list[Person]:
    df = _read_df(path)
    cmap = _column_map(list(df.columns))
    rid_col = _row_id_column(df)
    truth_col = _truth_column(df)

    # Drop rows missing a LinkedIn URL — they're useless for our cascade
    if "linkedin_url" in cmap:
        df = df[df[cmap["linkedin_url"]].notna() & (df[cmap["linkedin_url"]] != "")]

    if sample and len(df) > sample:
        df = df.sample(n=sample, random_state=seed).reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)

    people: list[Person] = []
    for idx, row in df.iterrows():
        rid = str(row[rid_col]) if rid_col else str(idx)
        kwargs: dict[str, Any] = {"row_id": rid}
        for field, src_col in cmap.items():
            val = row.get(src_col)
            if pd.isna(val) or val == "":
                continue
            kwargs[field] = str(val).strip()

        if kwargs.get("school") and not kwargs.get("school_domain"):
            dom = resolve_domain(kwargs["school"])
            if dom:
                kwargs["school_domain"] = dom

        skip_cols = set(cmap.values()) | ({truth_col} if truth_col else set()) | ({rid_col} if rid_col else set())
        extra = {c: row[c] for c in df.columns if c not in skip_cols and pd.notna(row[c]) and row[c] != ""}
        if extra:
            kwargs["extra"] = {k: str(v) for k, v in extra.items()}
        people.append(Person(**kwargs))
    return people


def read_truth(path: Path) -> dict[str, set[str]]:
    """Return {row_id: {email1, email2, ...}} from a CSV/TSV with a truth column.

    Pipe-separated `all_emails` values are split. Returns lowercase emails.
    """
    df = _read_df(path)
    rid_col = _row_id_column(df)
    truth_col = _truth_column(df)
    if not truth_col:
        return {}

    out: dict[str, set[str]] = {}
    for idx, row in df.iterrows():
        rid = str(row[rid_col]) if rid_col else str(idx)
        val = row.get(truth_col)
        if pd.isna(val) or val == "":
            continue
        emails = {e.strip().lower() for e in str(val).split("|") if "@" in e}
        # If there's also a primary_email column, fold that in too
        for c in df.columns:
            if _norm(c) in ("primaryemail", "email") and c != truth_col:
                v = row.get(c)
                if pd.notna(v) and v and "@" in str(v):
                    emails.add(str(v).strip().lower())
        if emails:
            out[rid] = emails
    return out


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
