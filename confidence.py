"""Confidence scorer for `(person_name, linkedin_slug, email)` triples.

The scraper's positional pairing is heuristic — same SERP container's
innerText regex'd for both an email and a slug. Often the snippet really
does belong to the right person; sometimes it's a directory page mixing many.

This module returns a 0–1 score (and component breakdown) so the caller can:
  • drop hits below a threshold
  • sort by confidence
  • report precision-at-K curves

Inputs: full name (string), slug (the part after /in/), email (full address).
Outputs: dict with named components + 'composite' final score.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


_NON_ALPHA = re.compile(r"[^a-z]")
_TRAIL_HEX = re.compile(r"-[a-f0-9]{6,}$")
_TRAIL_NUM = re.compile(r"-?\d{4,}$")


def _normalize(s: str) -> str:
    """Lowercase, strip diacritics, drop non-alpha."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return _NON_ALPHA.sub("", s.lower())


def _slug_clean(slug: str) -> str:
    """Strip trailing identifier suffixes (`-aa74b724`, `-12345678`)."""
    s = slug.lower().strip()
    s = _TRAIL_HEX.sub("", s)
    s = _TRAIL_NUM.sub("", s)
    return s


def _name_tokens(name: str) -> tuple[str, str]:
    """Return (first, last) lowercased, ascii-only, no spaces."""
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return _normalize(parts[0]), ""
    return _normalize(parts[0]), _normalize(parts[-1])


def _ratio(a: str, b: str) -> float:
    """Sequence-similarity ratio in [0, 1]."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def score(name: str, slug: str, email: str) -> dict:
    """Score the (name, slug, email) triple. Returns a dict.

    Components are bounded [0, 1]; `composite` is bounded [0, 1].
    """
    if not email or "@" not in email:
        return {"composite": 0.0, "reason": "no email"}

    local, _, domain = email.partition("@")
    local_n = _normalize(local)
    slug_clean = _slug_clean(slug)
    slug_n = _normalize(slug_clean)
    first, last = _name_tokens(name)
    name_full = first + last
    name_dot_n = _normalize(f"{first}.{last}")  # same as name_full after normalize

    cmp = {
        # --- exact / containment ---
        # Slug literally equals local-part (e.g. "vipulved" ↔ "vipulved@…")
        "slug_eq_local":      1.0 if slug_n == local_n else 0.0,
        "slug_in_local":      1.0 if slug_n and slug_n in local_n else 0.0,
        "local_in_slug":      1.0 if local_n and local_n in slug_n else 0.0,

        # --- name-based ---
        "first_in_local":     1.0 if first and first in local_n else 0.0,
        "last_in_local":      1.0 if last and last in local_n else 0.0,
        "fullname_in_local":  1.0 if name_full and name_full in local_n else 0.0,
        "fi_lastname":        1.0 if (first and last
                                       and local_n.startswith(first[0] + last)) else 0.0,
        "first_lastname":     1.0 if (first and last
                                       and local_n.startswith(first + last)) else 0.0,
        "first_dot_last":     1.0 if (first and last
                                       and local_n == name_dot_n) else 0.0,

        # --- fuzzy fallback ---
        "ratio_local_slug":   round(_ratio(local_n, slug_n), 3),
        "ratio_local_name":   round(_ratio(local_n, name_full), 3),
    }

    # Composite: take the strongest binary signal first; fall back to fuzzy.
    binary_keys = [
        "slug_eq_local", "first_dot_last", "fullname_in_local",
        "first_lastname", "fi_lastname",
    ]
    if any(cmp[k] >= 1.0 for k in binary_keys):
        composite = 1.0
    elif cmp["slug_in_local"] >= 1.0 or cmp["local_in_slug"] >= 1.0:
        composite = 0.85
    elif cmp["last_in_local"] and cmp["first_in_local"]:
        composite = 0.75
    elif cmp["last_in_local"] or cmp["first_in_local"]:
        composite = 0.55
    else:
        # Pure fuzzy — usually noise, but keep some signal
        fuzzy = max(cmp["ratio_local_slug"], cmp["ratio_local_name"])
        composite = round(fuzzy * 0.5, 2)  # cap at 0.5 since it's purely fuzzy

    cmp["composite"] = composite
    cmp["bucket"] = (
        "HIGH"   if composite >= 0.8
        else "MED" if composite >= 0.55
        else "LOW"
    )
    return cmp


# Sanity tests as a CLI: `python confidence.py`
if __name__ == "__main__":
    cases = [
        # (name, slug, email, expected_bucket)
        ("Vipul Ved",     "vipulved",            "vipulved@gmail.com",          "HIGH"),
        ("Hugo Davies",   "davieshugo",          "davies.hugo1@gmail.com",      "HIGH"),
        ("Scott Thomas",  "scott-thomas-jr",     "scott.thomas777@gmail.com",   "HIGH"),
        ("Hrishikesh Bopalkar", "hrishikeshbopalkar", "hbopalkar@gmail.com",   "HIGH"),
        ("Joseph Brown",  "josephhbrown",        "3lk3rd@gmail.com",            "LOW"),
        ("Jorge Concepcion", "jorge-concepcion-aa74b724", "corporatebusinessgrp@gmail.com", "LOW"),
        ("Sylvia Cheng",  "sylvia-si-cheng",     "scheng@berkeley.edu",         "MED-or-better"),
        ("Naomi Wong",    "naomiwong19",         "nwongg@berkeley.edu",         "MED-or-better"),
        ("Talha Faiz",    "talhafaiz",           "faiz.t@candidintelligence.com", "MED-or-better"),
    ]
    for name, slug, email, expected in cases:
        s = score(name, slug, email)
        ok = "✓" if (s["bucket"] == expected
                     or (expected == "MED-or-better" and s["bucket"] in ("MED", "HIGH"))) else "✗"
        print(f"{ok} {name:25s} ↔ {email:38s} = {s['bucket']:4s} ({s['composite']})")
