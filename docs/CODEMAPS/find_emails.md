# find_emails Codemap

**Last Updated:** 2026-05-07
**Entry Point:** `find_emails.py` (single file, repo root)
**Runtime:** Python 3.10+, async via `asyncio` + `httpx`

## Architecture

```
caller
  │
  ├── find_email(url)            ── single-URL convenience wrapper
  └── find_emails(urls, ...)     ── batch entry point (pandas DataFrame out)
        │
        ▼
  _normalize_urls          add scheme, drop blanks
        │
        ▼
  _find_emails_async       chunk + fan-out, asyncio.Semaphore(parallel)
        │
        ▼
  _call_actor (per chunk)  POST run → poll status → GET dataset items
        │                  Apify actor: harvestapi/linkedin-profile-scraper
        ▼
  _profile_slug            match results back to inputs by /in/<slug>
  _extract_emails          rank: status=valid > risky, then qualityScore desc
  _extract_company / _full_name
        │
        ▼
  _build_dataframe         one row per input URL, original order preserved

CLI: main()  argparse → read CSV → find_emails(...) → merge originals → write CSV
```

## Public API

| Symbol | Kind | Purpose |
|-|-|-|
| `find_email(url, *, apify_token=None, actor_timeout_s=1800)` | function | Single URL → email string or None |
| `find_emails(urls, *, apify_token=None, chunk_size=50, parallel=5, actor_timeout_s=1800, progress=True)` | function | Batch → DataFrame (one row per input URL) |
| `main()` | function | CLI entry, also wired as `find-emails` console script |
| `ACTOR_ID = "harvestapi/linkedin-profile-scraper"` | constant | The only actor used |

## DataFrame schema

| Column | Type | Notes |
|-|-|-|
| `linkedin_url` | str | Input URL, normalized (https:// added if missing) |
| `email` | str | Top-ranked candidate (`""` if none) |
| `all_emails` | str | Semicolon-separated, ranked order |
| `email_status` | str | `valid` / `risky` / `invalid` / `""` |
| `email_quality` | int/str | harvestapi `qualityScore` |
| `name` | str | From `name` or `firstName + lastName` |
| `headline` | str | LinkedIn headline |
| `company` | str | From `currentPosition.companyName` (dict or list[0]) |
| `raw` | str | JSON-encoded actor item, for downstream reuse |

## Internal helpers

| Function | Purpose |
|-|-|
| `_normalize_urls` | Strip, drop empties, prepend `https://` if missing |
| `_find_emails_async` | Chunk URLs, gather actor calls under a Semaphore |
| `_call_actor` | Apify lifecycle: `POST /acts/{id}/runs` → poll `/actor-runs/{id}` until terminal → `GET /datasets/{id}/items` |
| `_profile_slug` | Pull `/in/<slug>` from `publicIdentifier` first, then URL-ish fields, then a regex over the JSON blob |
| `_slug_from_url` | Regex extract `/in/<slug>` from a URL |
| `_extract_emails` | Read `emails` field, rank by `status` then `qualityScore` |
| `_extract_company` | Handle dict vs list shapes for `currentPosition` |
| `_full_name` | Prefer `name`, fall back to first+last |
| `_build_dataframe` | Map slug → input URL preserving order; emit empty strings on miss |
| `_empty_frame` | Shared empty-DataFrame schema |

## Apify call shape

```
POST  /v2/acts/harvestapi~linkedin-profile-scraper/runs?token=...
      body: {"profileScraperMode": "Profile details + email search ($10 per 1k)",
             "queries": [<urls>]}
GET   /v2/actor-runs/{run_id}?token=...           (every 5s until terminal)
GET   /v2/datasets/{ds_id}/items?token=...&format=json&clean=true
```

Failed actor runs (FAILED / ABORTED / TIMED-OUT / past deadline) return `[]` — silently skipped, the affected URLs end up with empty rows.

## Defaults & limits

| Setting | Default | Notes |
|-|-|-|
| `DEFAULT_CHUNK_SIZE` | 50 | Bake-off proved 50 fits in the 30-min actor timeout |
| `DEFAULT_PARALLEL` | 5 | Apify allows up to 25 concurrent runs |
| `DEFAULT_TIMEOUT_S` | 1800 | Per-actor-run wall clock |
| `httpx` client timeout | `actor_timeout_s + 30` | Outer envelope |

## External dependencies

| Package | Why |
|-|-|
| `httpx` | Async HTTP client for the Apify API |
| `pandas` | Output DataFrame + CSV I/O in CLI |
| `python-dotenv` | Load `APIFY_TOKEN` from `.env` (optional, only in CLI path) |

## Related files

- `scripts/actor_bakeoff.py` — standalone harness used to compare candidate actors against ground truth (no project deps)
- `pyproject.toml` — declares the `find-emails` console script
- `.env.example` — `APIFY_TOKEN` setup
- `data/input/`, `data/output/`, `data/ground_truth/` — local CSV scratch area (gitignored)
