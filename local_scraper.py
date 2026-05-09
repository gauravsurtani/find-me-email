"""Self-hosted recreation of `bhansalisoft/linkedin-email-scraper`.

Mirrors the actor's logic (search-engine dorks for `site:linkedin.com/ "@<domain>" <keyword>`
+ email regex on result snippets/HTML) but runs locally with httpx + BeautifulSoup,
no Selenium, no Apify subscription.

Supports two backends:
  - duckduckgo : html.duckduckgo.com (default — public HTML SERP, no API key, lenient)
  - google     : www.google.com/search (often returns CAPTCHA after a few queries
                 from the same IP without proxies; included for honest comparison)

Honest limitations vs the actor:
  - We do NOT use undetected-chromedriver, so Google will rate-limit fast.
  - We do NOT visit linkedin.com directly (it's auth-walled) — we read what the
    search engine has indexed in result snippets, same as the actor does.
  - LinkedIn aggressively scrubs emails from public profiles, so the practical
    yield is low whether you self-host OR pay $10/mo. This is a property of the
    data source, not the scraper.

Usage:
    python local_scraper.py --keyword "CEO" --location "San Francisco" \
        --email-domain "@gmail.com" --limit 20

    # Search by name (closest analog to looking up one person):
    python local_scraper.py --keyword "Satya Nadella" --email-domain "@microsoft.com"

    # Custom domain (e.g. company / school):
    python local_scraper.py --keyword "engineer" --email-domain "@stanford.edu"

    # Compare backends:
    python local_scraper.py --keyword "CEO" --backend google --limit 10
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus, urlparse, parse_qs, unquote

import httpx
from bs4 import BeautifulSoup

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
LINKEDIN_PROFILE_RX = re.compile(r"linkedin\.com/(?:in|pub)/([a-zA-Z0-9\-_%]+)", re.IGNORECASE)

USER_AGENTS = [
    # Recent stable Chrome on macOS / Windows / Linux
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:124.0) "
    "Gecko/20100101 Firefox/124.0",
]


@dataclass
class Hit:
    email: str = ""
    linkedin_slug: str = ""
    linkedin_url: str = ""
    snippet: str = ""
    title: str = ""
    result_url: str = ""
    source_engine: str = ""

    def merge(self, other: "Hit") -> None:
        # Prefer non-empty values from `other` (later result with more info).
        for f in ("linkedin_slug", "linkedin_url", "snippet", "title", "result_url"):
            if not getattr(self, f) and getattr(other, f):
                setattr(self, f, getattr(other, f))


@dataclass
class RunResult:
    backend: str
    query: str
    pages_fetched: int = 0
    raw_html_bytes: int = 0
    blocked: bool = False
    block_reason: str = ""
    hits: list[Hit] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Query builder (mirrors the bhansali actor's dork)
# ─────────────────────────────────────────────────────────────────────────────

def build_query(*, keyword: str, location: str, email_domain: str) -> str:
    """Build a Google-dork-style query.

    Reproduces what the bhansali actor builds:
        site:linkedin.com/ "@gmail.com" "CEO" "San Francisco"
    """
    parts = ['site:linkedin.com/']
    if email_domain:
        parts.append(f'"{email_domain}"')
    if keyword:
        parts.append(f'"{keyword}"')
    if location:
        parts.append(f'"{location}"')
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Backends
# ─────────────────────────────────────────────────────────────────────────────

def _client(timeout_s: int = 30) -> httpx.Client:
    return httpx.Client(
        timeout=timeout_s,
        follow_redirects=True,
        headers={"User-Agent": random.choice(USER_AGENTS),
                 "Accept-Language": "en-US,en;q=0.9"},
    )


def fetch_duckduckgo(query: str, *, max_pages: int, page_delay_s: float = 2.0) -> RunResult:
    """DuckDuckGo HTML endpoint. No API key. Permissive of scraping.

    Pagination uses the `s` (start offset) and `dc` (form continuation) form fields.
    """
    res = RunResult(backend="duckduckgo", query=query)
    base = "https://html.duckduckgo.com/html/"
    s = 0
    seen_urls: set[str] = set()

    with _client() as client:
        for page in range(max_pages):
            params = {"q": query, "kl": "us-en", "s": str(s)}
            try:
                r = client.get(base, params=params)
            except httpx.HTTPError as e:
                res.blocked = True
                res.block_reason = f"network: {type(e).__name__}: {e}"
                return res

            res.pages_fetched += 1
            res.raw_html_bytes += len(r.content)

            if r.status_code not in (200, 202):
                res.blocked = True
                res.block_reason = f"http {r.status_code}"
                return res
            soup = BeautifulSoup(r.text, "html.parser")
            results = soup.select("div.result, div.web-result")
            # DDG's 202 rate-limit page is a stub with NO result divs. Only
            # treat this as blocked if 202 AND we got no results back.
            if r.status_code == 202 and not results:
                res.blocked = True
                res.block_reason = "202 rate-limit stub (no result divs)"
                return res
            if "anomaly" in r.text.lower() or "captcha" in r.text.lower()[:5000]:
                res.blocked = True
                res.block_reason = "captcha/anomaly page"
                return res
            if not results:
                # Empty page — end of results.
                break

            new_this_page = 0
            for div in results:
                a = div.select_one("a.result__a, a.result-link")
                snippet = (div.select_one("a.result__snippet, div.result__snippet")
                           or div.select_one("[class*=snippet]"))
                title = a.get_text(strip=True) if a else ""
                href = _extract_ddg_href(a.get("href") if a else "")
                snip_text = snippet.get_text(" ", strip=True) if snippet else ""
                full_text = f"{title} {snip_text}"

                if href in seen_urls:
                    continue
                seen_urls.add(href)
                new_this_page += 1

                hit = _hit_from_text_and_url(full_text, href, title, snip_text, "duckduckgo")
                if hit:
                    res.hits.append(hit)

            if new_this_page == 0:
                break
            s += 30  # DDG returns ~30 per page
            time.sleep(page_delay_s + random.random())

    return res


def fetch_google(query: str, *, max_pages: int, page_delay_s: float = 3.0) -> RunResult:
    """Google direct. Without Selenium / proxies, expect CAPTCHA quickly.

    Included for honest comparison with the bhansali actor.
    """
    res = RunResult(backend="google", query=query)
    base = "https://www.google.com/search"
    seen_urls: set[str] = set()

    with _client() as client:
        for page in range(max_pages):
            params = {"q": query, "start": str(page * 10), "hl": "en", "gl": "us"}
            try:
                r = client.get(base, params=params)
            except httpx.HTTPError as e:
                res.blocked = True
                res.block_reason = f"network: {type(e).__name__}: {e}"
                return res

            res.pages_fetched += 1
            res.raw_html_bytes += len(r.content)

            if r.status_code in (429, 503) or "/sorry/" in str(r.url):
                res.blocked = True
                res.block_reason = f"http {r.status_code} or /sorry/ redirect"
                return res
            if "detected unusual traffic" in r.text.lower():
                res.blocked = True
                res.block_reason = "unusual-traffic interstitial"
                return res

            soup = BeautifulSoup(r.text, "html.parser")
            # Google uses several result containers. Be forgiving.
            results = soup.select("div.g, div.tF2Cxc, div[data-snhf]")
            if not results:
                break

            new_this_page = 0
            for div in results:
                a = div.select_one("a")
                href = a.get("href", "") if a else ""
                if not href.startswith("http"):
                    continue
                title_el = div.select_one("h3")
                title = title_el.get_text(strip=True) if title_el else ""
                snip = div.get_text(" ", strip=True)

                if href in seen_urls:
                    continue
                seen_urls.add(href)
                new_this_page += 1

                hit = _hit_from_text_and_url(snip, href, title, snip, "google")
                if hit:
                    res.hits.append(hit)

            if new_this_page == 0:
                break
            time.sleep(page_delay_s + random.random())

    return res


# ─────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_ddg_href(href: str) -> str:
    """DuckDuckGo HTML wraps results in a redirect like //duckduckgo.com/l/?uddg=..."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def _hit_from_text_and_url(text: str, url: str, title: str, snippet: str,
                            engine: str) -> Hit | None:
    """Build a Hit if we can extract an email or a LinkedIn slug."""
    emails = EMAIL_RX.findall(text)
    li = LINKEDIN_PROFILE_RX.search(url) or LINKEDIN_PROFILE_RX.search(text)
    if not emails and not li:
        return None
    email = emails[0].lower() if emails else ""
    slug = li.group(1).lower() if li else ""
    li_url = f"https://linkedin.com/in/{slug}" if slug else ""
    return Hit(
        email=email, linkedin_slug=slug, linkedin_url=li_url,
        snippet=snippet[:400], title=title, result_url=url,
        source_engine=engine,
    )


def dedupe_hits(hits: Iterable[Hit]) -> list[Hit]:
    """Merge by (email or linkedin_slug). Prefer rows that have both."""
    by_key: dict[str, Hit] = {}
    for h in hits:
        key = h.email or f"li:{h.linkedin_slug}"
        if not key:
            continue
        if key in by_key:
            by_key[key].merge(h)
        else:
            by_key[key] = h
    # Sort: rows with both email + linkedin first
    return sorted(
        by_key.values(),
        key=lambda h: (0 if (h.email and h.linkedin_slug) else 1, h.email),
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--keyword", required=True,
                    help='Free-text keyword (e.g. "CEO", "Satya Nadella")')
    ap.add_argument("--location", default="",
                    help='Optional location (e.g. "San Francisco")')
    ap.add_argument("--email-domain", default="@gmail.com",
                    help='Email domain to dork for. Default: @gmail.com')
    ap.add_argument("--backend", default="duckduckgo",
                    choices=["duckduckgo", "google", "both"])
    ap.add_argument("--limit", type=int, default=50,
                    help="Stop after this many unique hits (0 = unlimited)")
    ap.add_argument("--max-pages", type=int, default=5,
                    help="Max SERP pages per backend")
    ap.add_argument("--out", default="data/output/local_scraper_run.json",
                    help="Where to write the run report")
    args = ap.parse_args()

    query = build_query(
        keyword=args.keyword, location=args.location, email_domain=args.email_domain,
    )

    print(f"Query : {query}")
    print(f"Backend: {args.backend}, max_pages={args.max_pages}, limit={args.limit}")
    print("-" * 70)

    runs: list[RunResult] = []
    backends = ["duckduckgo", "google"] if args.backend == "both" else [args.backend]
    for backend in backends:
        print(f"\n[{backend}] starting...")
        t0 = time.time()
        if backend == "duckduckgo":
            res = fetch_duckduckgo(query, max_pages=args.max_pages)
        else:
            res = fetch_google(query, max_pages=args.max_pages)
        elapsed = time.time() - t0

        print(f"[{backend}] pages={res.pages_fetched}, html_bytes={res.raw_html_bytes:,}, "
              f"hits={len(res.hits)}, elapsed={elapsed:.1f}s")
        if res.blocked:
            print(f"[{backend}] BLOCKED: {res.block_reason}")
        runs.append(res)

    # Combine + dedupe
    all_hits = dedupe_hits([h for r in runs for h in r.hits])
    if args.limit:
        all_hits = all_hits[: args.limit]

    print("\n" + "=" * 70)
    print(f"TOTAL UNIQUE HITS: {len(all_hits)}")
    n_email = sum(1 for h in all_hits if h.email)
    n_linked = sum(1 for h in all_hits if h.linkedin_slug)
    print(f"  with email      : {n_email}")
    print(f"  with linkedin   : {n_linked}")
    print(f"  with BOTH       : {sum(1 for h in all_hits if h.email and h.linkedin_slug)}")

    print("\nFirst 10 hits:")
    for h in all_hits[:10]:
        print(f"  [{h.source_engine[:3]}] {h.email or '(no email)':35s} "
              f"{h.linkedin_url or '(no linkedin)'}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "query": query,
        "backend": args.backend,
        "runs": [
            {**{k: v for k, v in asdict(r).items() if k != "hits"},
             "n_hits": len(r.hits)} for r in runs
        ],
        "n_unique_hits": len(all_hits),
        "hits": [asdict(h) for h in all_hits],
    }, indent=2, default=str))
    print(f"\nWrote {out}")

    # Exit non-zero only if every backend was blocked AND we got nothing.
    all_blocked = runs and all(r.blocked for r in runs)
    return 1 if all_blocked and not all_hits else 0


if __name__ == "__main__":
    sys.exit(main())
