"""College name → email-domain resolver.

Strategy:
1. Direct lookup in a small curated map (extend as needed).
2. Heuristic: if school name contains a known token, map it.
3. Fallback: lowercase, strip stopwords, join, append ".edu" — best-effort.
"""
from __future__ import annotations

import re

# Curated map for top schools likely to appear in the dataset (extend freely).
KNOWN: dict[str, str] = {
    "stanford university": "stanford.edu",
    "stanford": "stanford.edu",
    "massachusetts institute of technology": "mit.edu",
    "mit": "mit.edu",
    "university of california berkeley": "berkeley.edu",
    "uc berkeley": "berkeley.edu",
    "berkeley": "berkeley.edu",
    "university of california los angeles": "ucla.edu",
    "ucla": "ucla.edu",
    "san jose state university": "sjsu.edu",
    "sjsu": "sjsu.edu",
    "santa clara university": "scu.edu",
    "scu": "scu.edu",
    "carnegie mellon university": "andrew.cmu.edu",
    "cmu": "andrew.cmu.edu",
    "harvard university": "harvard.edu",
    "harvard": "harvard.edu",
    "yale university": "yale.edu",
    "princeton university": "princeton.edu",
    "columbia university": "columbia.edu",
    "cornell university": "cornell.edu",
    "university of washington": "uw.edu",
    "university of michigan": "umich.edu",
    "georgia tech": "gatech.edu",
    "georgia institute of technology": "gatech.edu",
    "university of illinois urbana champaign": "illinois.edu",
    "uiuc": "illinois.edu",
    "university of texas at austin": "utexas.edu",
    "ut austin": "utexas.edu",
    "new york university": "nyu.edu",
    "nyu": "nyu.edu",
    "northeastern university": "northeastern.edu",
    "university of southern california": "usc.edu",
    "usc": "usc.edu",
    "purdue university": "purdue.edu",
    "university of pennsylvania": "upenn.edu",
    "upenn": "seas.upenn.edu",
    "university of california san diego": "ucsd.edu",
    "ucsd": "ucsd.edu",
    "university of california davis": "ucdavis.edu",
    "uc davis": "ucdavis.edu",
    "university of california santa cruz": "ucsc.edu",
    "ucsc": "ucsc.edu",
    "university of california irvine": "uci.edu",
    "uc irvine": "uci.edu",
}

STOPWORDS = {"university", "of", "the", "college", "institute", "school", "and"}
LINKEDIN_SCHOOL_RE = re.compile(r"linkedin\.com/school/([^/?#]+)")


def resolve_domain(school: str | None) -> str | None:
    if not school:
        return None
    s = school.strip().lower()

    # LinkedIn school URL?
    m = LINKEDIN_SCHOOL_RE.search(s)
    if m:
        slug = m.group(1).replace("-", " ")
        return KNOWN.get(slug) or _heuristic(slug)

    # Already a domain?
    if "." in s and " " not in s and "/" not in s:
        return s

    if s in KNOWN:
        return KNOWN[s]

    for key, dom in KNOWN.items():
        if key in s:
            return dom

    return _heuristic(s)


def _heuristic(s: str) -> str | None:
    tokens = [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in STOPWORDS]
    if not tokens:
        return None
    return "".join(tokens[:2]) + ".edu"
