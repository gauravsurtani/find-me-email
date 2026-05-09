"""Self-hosted recreation of `bhansalisoft/linkedin-email-scraper`, BROWSER edition.

Uses Playwright + Chromium with stealth patches so we can bypass the
JS-required interstitial that blocks plain HTTP scraping. This is the same
approach the bhansali actor uses (undetected-chromedriver), only freer.

Stealth tricks applied:
  • Realistic User-Agent (latest Chrome on macOS)
  • 1280x800 viewport (common, not the headless default)
  • --disable-blink-features=AutomationControlled
  • navigator.webdriver = undefined
  • navigator.plugins = fake non-empty array
  • navigator.languages = ['en-US', 'en']
  • Random delays between actions (1.0–2.5s)

Backends: google (primary), duckduckgo, bing.

Usage:
    # Setup once:
    pip install playwright && playwright install chromium

    # Real run:
    python browser_scraper.py --keyword "CEO" --location "San Francisco" \
        --email-domain "@gmail.com" --backend google --max-pages 3

    # Debug a block: open headed browser + screenshot every page
    python browser_scraper.py --keyword "CEO" --backend google \
        --no-headless --screenshot
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
from urllib.parse import quote_plus, urlparse, parse_qs, unquote

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
LINKEDIN_RX = re.compile(r"linkedin\.com/(?:in|pub)/([a-zA-Z0-9\-_%]+)", re.IGNORECASE)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
const originalQuery = navigator.permissions ? navigator.permissions.query : null;
if (originalQuery) {
  navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters);
}
"""


@dataclass
class Hit:
    email: str = ""
    linkedin_slug: str = ""
    linkedin_url: str = ""
    title: str = ""
    snippet: str = ""
    result_url: str = ""
    source_engine: str = ""

    def merge(self, other: "Hit") -> None:
        for f in ("linkedin_slug", "linkedin_url", "title", "snippet", "result_url"):
            if not getattr(self, f) and getattr(other, f):
                setattr(self, f, getattr(other, f))


@dataclass
class RunResult:
    backend: str
    query: str
    pages_fetched: int = 0
    blocked: bool = False
    block_reason: str = ""
    final_url: str = ""
    hits: list[Hit] = field(default_factory=list)
    screenshot_paths: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_query(*, keyword: str, location: str, email_domain: str) -> str:
    parts = ['site:linkedin.com/']
    if email_domain:
        parts.append(f'"{email_domain}"')
    if keyword:
        parts.append(f'"{keyword}"')
    if location:
        parts.append(f'"{location}"')
    return " ".join(parts)


def hit_from(text: str, url: str, title: str, snippet: str, engine: str) -> Hit | None:
    emails = EMAIL_RX.findall(text)
    li = LINKEDIN_RX.search(url) or LINKEDIN_RX.search(text)
    if not emails and not li:
        return None
    return Hit(
        email=(emails[0].lower() if emails else ""),
        linkedin_slug=(li.group(1).lower() if li else ""),
        linkedin_url=(f"https://linkedin.com/in/{li.group(1).lower()}" if li else ""),
        title=title, snippet=snippet[:400], result_url=url,
        source_engine=engine,
    )


def dedupe(hits: list[Hit]) -> list[Hit]:
    by_key: dict[str, Hit] = {}
    for h in hits:
        key = h.email or f"li:{h.linkedin_slug}"
        if not key:
            continue
        if key in by_key:
            by_key[key].merge(h)
        else:
            by_key[key] = h
    return sorted(by_key.values(),
                  key=lambda h: (0 if (h.email and h.linkedin_slug) else 1, h.email))


def jitter(a: float = 1.0, b: float = 2.5) -> None:
    time.sleep(a + random.random() * (b - a))


# ─────────────────────────────────────────────────────────────────────────────
# Browser session
# ─────────────────────────────────────────────────────────────────────────────

def make_browser(p, *, headless: bool):
    """Launch chromium with anti-detection flags."""
    return p.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--start-maximized",
        ],
    )


def make_context(browser):
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        timezone_id="America/Los_Angeles",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    ctx.add_init_script(STEALTH_INIT_SCRIPT)
    return ctx


def maybe_dismiss_consent(page) -> None:
    """Best-effort EU/cookie consent dismissal so we can read results."""
    selectors = [
        'button:has-text("Accept all")',
        'button:has-text("I agree")',
        'button:has-text("Reject all")',
        'button[aria-label*="Accept"]',
        '#L2AGLb',  # Google EU consent button id
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=500):
                btn.click()
                jitter(0.5, 1.5)
                return
        except Exception:
            continue


def detect_block(page) -> str:
    """Returns block reason string if blocked, else empty string."""
    url = page.url
    if "/sorry/" in url or "/recaptcha/" in url:
        return f"redirected to {url}"
    body_text = page.evaluate("document.body ? document.body.innerText : ''") or ""
    body_lower = body_text.lower()[:5000]
    if "unusual traffic" in body_lower:
        return "unusual-traffic interstitial"
    if "i'm not a robot" in body_lower or "i am not a robot" in body_lower:
        return "captcha challenge"
    if "one last step" in body_lower or "please solve the challenge" in body_lower:
        return "bing captcha challenge"
    if "unexpected error" in body_lower and "duckduckgo" in body_lower:
        return "ddg soft-block (unexpected error page)"
    if "before you continue" in body_lower and "google" in body_lower:
        return "google consent wall (couldn't dismiss)"
    return ""


def screenshot(page, dest: Path, label: str) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{int(time.time())}_{label}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Per-engine scrapers (browser-based)
# ─────────────────────────────────────────────────────────────────────────────

def scrape_google(page, query: str, *, max_pages: int, snap_dir: Path | None,
                   res: RunResult) -> None:
    page.goto(f"https://www.google.com/search?q={quote_plus(query)}&hl=en&gl=us",
              wait_until="domcontentloaded", timeout=30000)
    maybe_dismiss_consent(page)

    for pageno in range(max_pages):
        res.pages_fetched += 1
        res.final_url = page.url
        block = detect_block(page)
        if block:
            res.blocked, res.block_reason = True, block
            if snap_dir:
                res.screenshot_paths.append(screenshot(page, snap_dir, f"google_blocked_p{pageno}"))
            return

        # Wait for results to render
        try:
            page.wait_for_selector("div.g, div.tF2Cxc, a[href*='linkedin.com/in']",
                                   timeout=10000)
        except Exception:
            pass

        if snap_dir:
            res.screenshot_paths.append(screenshot(page, snap_dir, f"google_p{pageno}"))

        # Extract results — multiple selectors for robustness
        items = page.evaluate("""
            () => {
                const results = [];
                const seen = new Set();
                const containers = document.querySelectorAll(
                    'div.g, div.tF2Cxc, div[data-snhf], div.MjjYud'
                );
                for (const c of containers) {
                    const a = c.querySelector('a[href^="http"]');
                    if (!a) continue;
                    const href = a.href;
                    if (seen.has(href)) continue;
                    seen.add(href);
                    const titleEl = c.querySelector('h3');
                    const title = titleEl ? titleEl.innerText : '';
                    const text = c.innerText || '';
                    results.push({href, title, text});
                }
                return results;
            }
        """) or []

        new_count = 0
        for item in items:
            h = hit_from(item["text"], item["href"], item["title"],
                         item["text"], "google")
            if h:
                res.hits.append(h)
                new_count += 1

        # Try to click "Next"
        try:
            next_btn = page.locator('a#pnnext, a[aria-label="Next page"]').first
            if next_btn.count() == 0 or pageno == max_pages - 1:
                break
            jitter(2.0, 4.0)
            next_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception as e:
            res.block_reason = f"next-click failed: {e}"
            break


def scrape_duckduckgo(page, query: str, *, max_pages: int, snap_dir: Path | None,
                       res: RunResult) -> None:
    page.goto(f"https://duckduckgo.com/?q={quote_plus(query)}",
              wait_until="domcontentloaded", timeout=30000)
    jitter(2, 3)
    for pageno in range(max_pages):
        res.pages_fetched += 1
        res.final_url = page.url
        if snap_dir:
            res.screenshot_paths.append(screenshot(page, snap_dir, f"ddg_p{pageno}"))

        items = page.evaluate("""
            () => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('article[data-testid="result"], li.react-results--main, div.result').forEach(c => {
                    const a = c.querySelector('a[href^="http"]');
                    if (!a) return;
                    const href = a.href;
                    if (seen.has(href)) return;
                    seen.add(href);
                    const titleEl = c.querySelector('h2 a, h3 a, .result__a');
                    const title = titleEl ? titleEl.innerText : '';
                    const text = c.innerText || '';
                    out.push({href, title, text});
                });
                return out;
            }
        """) or []

        for item in items:
            h = hit_from(item["text"], item["href"], item["title"],
                         item["text"], "duckduckgo")
            if h:
                res.hits.append(h)

        # DDG doesn't paginate via URL — it has a "More results" button
        try:
            more = page.locator('button:has-text("More results")').first
            if more.count() == 0:
                break
            jitter(2, 4)
            more.click()
            page.wait_for_timeout(2500)
        except Exception:
            break


def scrape_bing(page, query: str, *, max_pages: int, snap_dir: Path | None,
                 res: RunResult) -> None:
    for pageno in range(max_pages):
        first = pageno * 10 + 1
        page.goto(f"https://www.bing.com/search?q={quote_plus(query)}&first={first}",
                  wait_until="domcontentloaded", timeout=30000)
        jitter(2, 3)
        res.pages_fetched += 1
        res.final_url = page.url
        if snap_dir:
            res.screenshot_paths.append(screenshot(page, snap_dir, f"bing_p{pageno}"))

        items = page.evaluate("""
            () => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('li.b_algo, div.b_title').forEach(c => {
                    const a = c.querySelector('a[href^="http"]');
                    if (!a) return;
                    const href = a.href;
                    if (seen.has(href)) return;
                    seen.add(href);
                    const titleEl = c.querySelector('h2 a, h2');
                    const title = titleEl ? titleEl.innerText : '';
                    const text = c.innerText || '';
                    out.push({href, title, text});
                });
                return out;
            }
        """) or []

        for item in items:
            h = hit_from(item["text"], item["href"], item["title"],
                         item["text"], "bing")
            if h:
                res.hits.append(h)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--location", default="")
    ap.add_argument("--email-domain", default="@gmail.com")
    ap.add_argument("--backend", default="google",
                    choices=["google", "duckduckgo", "bing", "all"])
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--no-headless", action="store_true",
                    help="Show the browser (helps fingerprint look real)")
    ap.add_argument("--no-stealth", action="store_true",
                    help="Skip playwright-stealth (use only inline tricks)")
    ap.add_argument("--no-warmup", action="store_true",
                    help="Skip the homepage-warmup before searching")
    ap.add_argument("--screenshot", action="store_true",
                    help="Save a screenshot of every page")
    ap.add_argument("--out", default="data/output/browser_scraper_run.json")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run:\n"
              "  pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    # Optional stronger stealth library
    Stealth = None
    if not args.no_stealth:
        try:
            from playwright_stealth import Stealth as _Stealth
            Stealth = _Stealth
        except ImportError:
            print("note: playwright-stealth not installed, using inline stealth only")

    query = build_query(
        keyword=args.keyword, location=args.location, email_domain=args.email_domain,
    )
    print(f"Query  : {query}")
    print(f"Backend: {args.backend} (headless={'no' if args.no_headless else 'yes'})")
    print("-" * 70)

    snap_dir = Path("data/output/screenshots") if args.screenshot else None
    backends = ["google", "duckduckgo", "bing"] if args.backend == "all" else [args.backend]

    runs: list[RunResult] = []

    def _do_runs(p):
        browser = make_browser(p, headless=not args.no_headless)
        ctx = make_context(browser)
        page = ctx.new_page()

        # Warmup: visit each engine's homepage first (looks more human, sets cookies)
        if not args.no_warmup:
            warmup_urls = {
                "google": "https://www.google.com/",
                "duckduckgo": "https://duckduckgo.com/",
                "bing": "https://www.bing.com/",
            }
            for backend in backends:
                try:
                    url = warmup_urls.get(backend)
                    if url:
                        page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        maybe_dismiss_consent(page)
                        jitter(2, 4)
                except Exception:
                    pass

        for backend in backends:
            print(f"\n[{backend}] starting...")
            res = RunResult(backend=backend, query=query)
            t0 = time.time()
            try:
                if backend == "google":
                    scrape_google(page, query, max_pages=args.max_pages,
                                  snap_dir=snap_dir, res=res)
                elif backend == "duckduckgo":
                    scrape_duckduckgo(page, query, max_pages=args.max_pages,
                                      snap_dir=snap_dir, res=res)
                elif backend == "bing":
                    scrape_bing(page, query, max_pages=args.max_pages,
                                snap_dir=snap_dir, res=res)
            except Exception as e:
                res.blocked, res.block_reason = True, f"exception: {type(e).__name__}: {e}"
            elapsed = time.time() - t0
            print(f"[{backend}] pages={res.pages_fetched}, hits={len(res.hits)}, "
                  f"blocked={res.blocked}, elapsed={elapsed:.1f}s")
            if res.blocked:
                print(f"[{backend}] block reason: {res.block_reason}")
            runs.append(res)

        ctx.close()
        browser.close()

    if Stealth is not None:
        with Stealth().use_sync(sync_playwright()) as p:
            _do_runs(p)
    else:
        with sync_playwright() as p:
            _do_runs(p)

    all_hits = dedupe([h for r in runs for h in r.hits])
    if args.limit:
        all_hits = all_hits[: args.limit]

    n_email = sum(1 for h in all_hits if h.email)
    n_linked = sum(1 for h in all_hits if h.linkedin_slug)
    n_both = sum(1 for h in all_hits if h.email and h.linkedin_slug)

    print("\n" + "=" * 70)
    print(f"TOTAL UNIQUE HITS: {len(all_hits)}")
    print(f"  with email      : {n_email}")
    print(f"  with linkedin   : {n_linked}")
    print(f"  with BOTH       : {n_both}")
    print("\nFirst 15 hits:")
    for h in all_hits[:15]:
        print(f"  [{h.source_engine[:3]}] {h.email or '(no email)':35s} "
              f"{h.linkedin_url or '(no linkedin)'}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "query": query,
        "backend": args.backend,
        "runs": [{**{k: v for k, v in asdict(r).items() if k != "hits"},
                  "n_hits": len(r.hits)} for r in runs],
        "n_unique_hits": len(all_hits),
        "n_with_email": n_email,
        "n_with_linkedin": n_linked,
        "n_with_both": n_both,
        "hits": [asdict(h) for h in all_hits],
    }, indent=2, default=str))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
