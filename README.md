# find-emails

Single-file Python module that takes a list of LinkedIn profile URLs and
returns emails. Wraps Apify's `harvestapi/linkedin-profile-scraper` actor —
the one that actually works.

```python
from find_emails import find_email, find_emails

email = find_email("https://linkedin.com/in/satyanadella")

df = find_emails([
    "https://linkedin.com/in/satyanadella",
    "https://linkedin.com/in/jeffweiner08",
])
# columns: linkedin_url, email, all_emails, email_status, email_quality,
#          name, headline, company, raw
```

## Drop into another project

Just copy [`find_emails.py`](find_emails.py) — it's one self-contained file.

```bash
pip install httpx pandas python-dotenv
export APIFY_TOKEN=apify_api_xxx        # get one at apify.com (FREE plan works)
```

Or `pip install -e .` from this repo to expose the `find-emails` CLI.

## CLI

```bash
python find_emails.py people.csv \
    --url-column linkedin_url \
    --output emails.csv
```

The output CSV has the email columns appended; original columns are preserved.

## How it works

| Step | Detail |
|-|-|
| 1 | Splits input URLs into chunks of 50 (configurable) |
| 2 | Launches up to 5 Apify actor runs in parallel (configurable, max 25) |
| 3 | Polls each run until SUCCEEDED, then pulls the dataset |
| 4 | Matches results back to inputs by LinkedIn slug (`/in/<slug>`) — robust to `www.` prefix differences |
| 5 | Returns one row per input URL, ordered as input |

Emails inside each result are ranked: `status=valid` first, then by `qualityScore`. The `email` column is the top pick; `all_emails` has all candidates.

## Cost & performance

| Metric | Value |
|-|-|
| Price | **$10 per 1000 LinkedIn URLs** (harvestapi pay-per-result) |
| Hit rate, working professionals | ~70% |
| Hit rate, students | ~50% |
| Throughput | ~250s per 50-URL chunk; ~30s per 5-URL chunk |
| Failed lookups | not billed |

`status='valid'` means the mailbox is SMTP-deliverable. `status='risky'` typically means a catch-all domain (mailbox unconfirmed but the domain accepts mail).

## What this replaced

This started as a 6-pass cascade through 6 different actors plus a verifier. After running on 2127 students:

| Approach | Hit rate | Cost | Notes |
|-|-|-|-|
| 6-pass cascade | 0% best-pick correct | ~$30+ | wrong-person noise; school directory pages dumped 2000+ emails per row |
| **harvestapi alone** | **47% with email, ~56% useful** | **$10** | clean, real LinkedIn DB results |

The cascade has been deleted; everything you need is in `find_emails.py`. See `scripts/actor_bakeoff.py` for the standalone harness used to compare actors.

## License

MIT.
