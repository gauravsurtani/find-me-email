# find-me-email

Pluggable person-to-email enrichment pipeline. Apify-first, with a cascade architecture so any new data source (Hunter, Exa, Apollo, RocketReach, Harmonic, etc.) drops in as a config change.

## Why

Given a CSV of people (LinkedIn URL + name + optional school/company), find their best email.
- **Cheap stages first** — pay-per-result actors, fall back to fuller scrapers, then pattern-guess as a last resort.
- **Transparent** — every email carries a `confidence` and `source_provider`. Pattern guesses are tagged `speculative` with an explicit "may not exist" warning.
- **Resumable** — per-row results are cached so a crash doesn't burn credits on rows already done.
- **Validatable** — bring a ground-truth CSV and get per-provider precision/recall + cost-per-correct-email.
- **Self-tuning** — accumulates per-provider hit rate stats; future versions reorder the cascade by archetype.

## Setup

```bash
cd find-me-email
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # then fill in APIFY_TOKEN
```

## Usage

```bash
# Sanity check the Apify token + see plan
find-me-email whoami

# How much would this cost (no API calls)
find-me-email estimate data/input/people.csv --sample 100

# Run the cascade on a 100-row random subset
find-me-email enrich data/input/people.csv --sample 100 --output data/output/poc.csv

# Validate against known emails (CSV with row_id,true_email)
find-me-email validate data/output/poc.csv data/ground_truth/known.csv

# See per-provider hit-rate stats
find-me-email stats
```

## Output schema

| column | meaning |
|---|---|
| `best_email` | top candidate, ranked by confidence |
| `best_confidence` | `high` (DB + verified) / `medium` (DB) / `low` (guess + verified) / `speculative` (guess) |
| `best_source` | which provider returned it |
| `best_verified` | true if SMTP/DNS-verified |
| `best_notes` | human notes — read this; speculative rows carry a "may not exist" warning |
| `all_candidates` | every email any provider returned, with confidence + source |
| `providers_attempted` | which providers ran |
| `total_cost_usd` | what this row cost |

## Adding a provider

1. Create `src/find_me_email/providers/<your_provider>.py` subclassing `EnrichmentProvider`.
2. Implement `enrich(person)` (or `enrich_batch(people)` if the API takes batches).
3. Register in `src/find_me_email/providers/__init__.py` (`REGISTRY`).
4. Add a stanza to `config/providers.yaml`.

Stubs already exist for `exa`, `apollo`, `hunter`, `harmonic` — fill in `enrich()` to enable.

## Compliance / ethics

You're responsible for how you use the output. CAN-SPAM, GDPR, and platform ToS apply. The `speculative` confidence tier exists specifically so a human can decide which guesses are worth verifying before any outreach.
