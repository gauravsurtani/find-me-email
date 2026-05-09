# Thesis: Is `bhansalisoft/linkedin-email-scraper` useful for find-me-email?

## TL;DR (final, empirical)

**No — and we now have ground-truth numbers proving it.**

A faithful self-hosted recreation (Playwright + `playwright-stealth` + DDG, no
subscription) was tested against `data/input/test_20.csv` (20 students with
known `primary_email`):

| Metric | Result |
|-|-|
| Returned an email for the target person | 14 / 20 (70%) |
| **Exact match to ground-truth email** | **0 / 20 (0%)** |
| Same domain only (right `@example.com`, wrong local-part) | 1 / 20 (Anu Kirk) |
| HIGH-confidence bucket | 0 hits |
| MED-confidence bucket | 7 hits, 0 correct (0% precision) |
| LOW-confidence bucket | 7 hits, 0 correct |

The recreation works mechanically — it pulls emails out of search-engine
snippets — but for the URL→email use case it produces **100% noise**.

---

## What we built (worktree contents)

| File | Purpose |
|-|-|
| `test_bhansali.py` | Harness that calls the *paid* actor's API (dry-run validated, never billed) |
| `local_scraper.py` | First attempt — `httpx` + BeautifulSoup. Empirically dead (Google JS-shell blocks) |
| `browser_scraper.py` | **Working recreation.** Playwright + Chromium + `playwright-stealth` + homepage warmup. Finds emails on DDG. |
| `confidence.py` | Scores `(name, slug, email)` triples 0–1 by name/local-part similarity. 9/9 self-tests pass |
| `pipeline.py` | End-to-end benchmark: runs scraper per row, scores hits, compares to ground truth |
| `data/output/pipeline_test_20.csv` | Per-row results from the 20-student run |

---

## Comparison: existing vs candidate vs self-host (final)

|   | `harvestapi/linkedin-profile-scraper` (current) | `bhansalisoft/linkedin-email-scraper` (paid) | `browser_scraper.py` (this worktree) |
|-|-|-|-|
| **Input** | LinkedIn profile URLs | Keyword + location + country + email-domain | Same as bhansali |
| **Output** | Email tied to a specific profile | Loose emails from Google SERPs | Loose emails from DDG SERPs |
| **Targeting** | Per-profile, deterministic | Population, heuristic | Population, heuristic |
| **Pricing** | $10 / 1k profiles, pay-per-result | $10 / month subscription | $0 + your time |
| **Targeted (URL→email) precision** | High (project depends on it) | Untested but architecturally same as us | **0/20 = 0%** |
| **Tech** | Apify-managed API | Selenium + undetected-chromedriver + Google | Playwright + stealth + DDG |
| **Reliability signals** | Production-grade | 1.7/5 stars, 22/137 runs aborted last 30d | Works on DDG, blocked by Google/Bing |

---

## Why every "found" email is wrong (mechanism)

For each student in `test_20.csv`, DDG's SERP returns a snippet that mentions
them. **The snippet often contains other people's emails** — recruiters
mentioning them, alumni directories with multiple contacts, "people you may
know" cross-references. The pipeline pairs the right LinkedIn URL with
the *first email in the same snippet block*, but that email belongs to
someone else.

| Student | Ground truth | Scraper returned | Why it failed |
|-|-|-|-|
| Anir Prativadi | `anir@valuemate.ai` | (no email) — but `amirallyear7@gmail.com` was nearby | Confused "Anir" with phonetically similar "Aamir/Amir" |
| Owen Burns | `owenb@toolcharm.com` | (no email) — `christinaowen@berkeley.edu` was nearby | Found people with surname "Owen", not first-name Owen |
| Janvi Jain | `janvijain@berkeley.edu` | (no email) — `arnavj@berkeley.edu` was nearby | Snippet mixed multiple Berkeley students |
| Anu Kirk | `anu.kirk@gmail.com` | `-anuravi8225@gmail.com` | Right domain, wrong person; also revealed an `EMAIL_RX` bug |

---

## Build journey (so the next person doesn't repeat it)

| Iteration | What we tried | Outcome |
|-|-|-|
| 1 | Plain `httpx` + BeautifulSoup (`local_scraper.py`) | Google + Bing return JS-required interstitials, no LinkedIn URLs in 91KB of HTML. Empirically dead. |
| 2 | Playwright Chromium, headless | DDG: "Unexpected error" page (silent block). Bing: "One last step" CAPTCHA. Google: `/sorry/` redirect. |
| 3 | Playwright + `playwright-stealth` + homepage warmup | **DDG: 14 emails / 30 unique results / 21s on 3 pages.** Bing + Google still blocked. |
| 4 | Same recreation, evaluated against 20 ground-truth rows | 0% exact match. Found-emails are real, but belong to other people. |

---

## Empirical summary — what we proved

1. **Plain HTTP scraping of search engines is dead in 2026.** Google + Bing serve JS-shell. ([proof: `<noscript>Please click here…`])
2. **Headless Chromium without stealth is detected instantly.** All three engines blocked us within seconds.
3. **Playwright + stealth + warmup beats DDG.** It's a real, free, working recreation of the actor's mechanism.
4. **The mechanism doesn't fit the URL→email use case.** SERP snippets aren't a reliable carrier for "this specific person's email." 0/20 precision is decisive.

---

## Verdict

| Use case | Recommendation |
|-|-|
| Drop-in replacement for `find_emails.py` | ❌ 0/20 precision proves wrong shape |
| Fallback when only a name is known | ❌ Same problem — we tested this exact case |
| Bulk harvesting "people in X with @Y email" | 🤷 Mechanism works, but yields noisy outbound-list quality. Not a contact-lookup tool. |
| Production integration (subscription) | ❌ $10/mo + 16% abort rate + Selenium fragility |
| Self-host (this worktree's recreation) | ❌ Works on DDG, but not for our use case |

**Recommendation: keep `harvestapi`.** The free recreation here is preserved
for reference (and as a useful demo of "Google scraping is harder than it
looks"), but should not be promoted into the main project.
