# Lessons

## 2026-05-09 — bhansalisoft/linkedin-email-scraper thesis test

### Question
Could `bhansalisoft/linkedin-email-scraper` (or a self-hosted recreation of it)
serve as an alternative or fallback to `harvestapi` for find-me-email?

### Verdict
**No.** Empirical: **0/20** exact-match precision against ground-truth
`primary_email` in `data/input/test_20.csv`. See worktree branch
`claude/bhansali-linkedin-test` for the full experiment.

### What we learned

1. **Architectural fit beats implementation effort.** The actor takes
   keyword + location + country; `find_emails.py` needs URL → email. That
   mismatch was decisive on day one. Building a working recreation didn't
   change the verdict — it only confirmed it. *Lesson: when an external tool's
   input shape doesn't match the use case's input shape, stop. No amount of
   stealth-Playwright cleverness fixes that.*

2. **"Found emails" ≠ "right emails."** A mechanically working scraper
   produced 14/20 hits; 0/20 were correct. The "we got real emails!"
   moment was a false positive that ground-truth correction caught.
   *Lesson: benchmark against ground truth before celebrating yield numbers.*

3. **Plain HTTP scraping of search engines is dead in 2026.** Google, Bing,
   and DDG all serve JS-required shells or rate-limit on first request from
   a non-residential IP. `httpx` + BeautifulSoup gets a `<noscript>Please
   click here…` interstitial. *Lesson: budget for a real browser from the
   start of any SERP-scraping plan.*

4. **`playwright-stealth` + homepage warmup is the minimum viable disguise.**
   Without those, even Playwright Chromium gets caught by `navigator.webdriver`
   and "no consent cookies" heuristics. With them, DDG let us through (Google
   and Bing did not). *Lesson: stealth library + warmup are non-optional.
   Headless plain Chromium is detected in <1 second.*

5. **SERP snippets are noisy carriers for targeted lookup.** A LinkedIn page
   snippet often contains emails belonging to recruiters/colleagues mentioning
   the target, not the target themselves. The email regex grabs the wrong
   person's address. *Lesson: positional `(slug, email)` pairing on snippet
   text is fundamentally untrustworthy. Cross-name disambiguation (Anir
   vs Aamir, Owen-as-firstname vs Owen-as-surname) collapses.*

6. **Self-host vs subscribe is not just a $-comparison.** Bhansali's $10/mo
   wraps proxies + captcha-solving + selector maintenance. DIY shifts those
   costs to engineering time. We avoided them entirely by accepting DDG-only
   coverage, but only because the verdict didn't depend on Google access.

### Decision

Keep `harvestapi` (URL → email, pay-per-result, ~50–70% hit rate).
Don't promote the recreation into the main project. If a name-only fallback
is later needed, use a structured people-search API (Apollo / Hunter /
RocketReach) rather than search-engine scraping.

### Reference

- Branch: `claude/bhansali-linkedin-test`
- Files (in worktree): `browser_scraper.py`, `confidence.py`, `pipeline.py`,
  `THESIS.md`
- Benchmark output: `data/output/pipeline_test_20.csv`
