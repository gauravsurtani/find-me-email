"""College name → email-domain resolver.

Strategy:
1. Direct lookup in a small curated map (extend as needed).
2. Heuristic: if school name contains a known token, map it.
3. Fallback: lowercase, strip stopwords, join, append ".edu" — best-effort.
"""
from __future__ import annotations

import re

# Curated map for top schools likely to appear in the dataset (extend freely).
# Values are the *primary* student-email domain. Schools with multiple subdomains
# (CMU has andrew.cmu.edu + tepper.cmu.edu, Yale has yale.edu + aya.yale.edu)
# are handled by additional_domains_for() below.
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
    "umich": "umich.edu",
    "university of michigan ann arbor": "umich.edu",
    "georgia tech": "gatech.edu",
    "georgia institute of technology": "gatech.edu",
    "university of illinois urbana champaign": "illinois.edu",
    "uiuc": "illinois.edu",
    "university of illinois at urbana champaign": "illinois.edu",
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
    "university of california riverside": "ucr.edu",
    "uc riverside": "ucr.edu",
    "ucr": "ucr.edu",
    # New additions from canary failures + common US schools
    "north carolina state university": "ncsu.edu",
    "nc state": "ncsu.edu",
    "ncsu": "ncsu.edu",
    "new mexico state university": "nmsu.edu",
    "nmsu": "nmsu.edu",
    "virginia tech": "vt.edu",
    "virginia polytechnic institute": "vt.edu",
    "vt": "vt.edu",
    "clemson university": "g.clemson.edu",
    "clemson": "g.clemson.edu",
    "texas a&m university": "tamu.edu",
    "texas a&m": "tamu.edu",
    "tamu": "tamu.edu",
    "arizona state university": "asu.edu",
    "asu": "asu.edu",
    "university of chicago": "uchicago.edu",
    "uchicago": "uchicago.edu",
    "chicago booth": "chicagobooth.edu",
    "booth school of business": "chicagobooth.edu",
    "university of virginia": "virginia.edu",
    "uva": "virginia.edu",
    "university at buffalo": "buffalo.edu",
    "suny buffalo": "buffalo.edu",
    "university of california san francisco": "ucsf.edu",
    "ucsf": "ucsf.edu",
    "university of san francisco": "usfca.edu",
    "san francisco state university": "sfsu.edu",
    "sfsu": "sfsu.edu",
    # International / scholar-friendly catch-alls
    "oxford university": "ox.ac.uk",
    "university of oxford": "ox.ac.uk",
    "cambridge university": "cam.ac.uk",
    "university of cambridge": "cam.ac.uk",
}


# Schools that commonly use multiple email subdomains for different programs.
# Pattern guesser will generate candidates against ALL listed domains for the school.
SUBDOMAIN_VARIANTS: dict[str, list[str]] = {
    "stanford.edu": ["stanford.edu", "cs.stanford.edu", "alumni.stanford.edu"],
    "berkeley.edu": ["berkeley.edu", "mba.berkeley.edu"],
    "andrew.cmu.edu": ["andrew.cmu.edu", "cmu.edu", "tepper.cmu.edu", "cs.cmu.edu"],
    "yale.edu": ["yale.edu", "aya.yale.edu"],
    "mit.edu": ["mit.edu", "alum.mit.edu", "sloan.mit.edu"],
    "harvard.edu": ["harvard.edu", "college.harvard.edu", "g.harvard.edu", "hbs.edu"],
    "columbia.edu": ["columbia.edu", "gsb.columbia.edu"],
    "northeastern.edu": ["northeastern.edu", "husky.neu.edu"],
}


def domains_for(school: str | None) -> list[str]:
    """Return ALL email-domain candidates for a school (primary + known subdomains)."""
    primary = resolve_domain(school)
    if not primary:
        return []
    variants = SUBDOMAIN_VARIANTS.get(primary, [primary])
    # Always include the primary first
    return [primary] + [v for v in variants if v != primary]

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


def extract_school_from_text(*texts: str | None) -> str | None:
    """Scan free-form text (headline, location, etc.) for any known school name.

    Returns the canonical school name if a known one is found, else None.
    Used when the source CSV has no `school` column but headlines like
    "CS @ Stanford" or "UC Berkeley | Researcher" carry the school inline.
    """
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack:
        return None
    # Sort longest-first so "san jose state university" matches before "san jose"
    for key in sorted(KNOWN, key=len, reverse=True):
        if key in haystack:
            return key
    return None


def _heuristic(s: str) -> str | None:
    tokens = [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in STOPWORDS]
    if not tokens:
        return None
    return "".join(tokens[:2]) + ".edu"
