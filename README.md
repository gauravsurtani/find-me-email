# find-emails

Find emails for LinkedIn profile URLs. Standalone tool that drops into any
Python project — and an add-on layer for plugging the same logic into a larger
application (e.g. a candidate drawer, outreach tool, CRM enrichment).

## What's in here

```
                       PURPOSE                          USAGE PATTERN
─────────────────────────────────────────────────────────────────────────────
  find_emails.py       Standalone Apify lookup          Batch CSV / scripts /
                       (cheap, ~50% hit rate,           notebooks / CLI
                        ~$10 per 1000 profiles)

  find_emails_         Standalone SignalHire lookup     Same — works alone
  signalhire.py        (richer data + personal email
                        + phone numbers, ~$0.06/lookup)

  reveal.py    ◀────── ADD-ON: thin orchestrator        Drop into a larger
                       Single function, source-list      app where reveals
                       parameter. Default: Apify only.   happen on user click

  scripts/             Standalone harness for adding
   actor_bakeoff.py    a new Apify actor — score it
                       against ground truth before
                       wiring it in
```

The two `find_emails*.py` files are independent — each works on its own.
`reveal.py` is a 100-line wrapper that calls them in caller-controlled order,
designed for "reveal one email per user click" use cases.

## Setup

```bash
pip install httpx pandas python-dotenv
export APIFY_TOKEN=apify_api_xxx           # for find_emails.py
export SIGNALHIRE_API_KEY=...              # for find_emails_signalhire.py
```

Or `pip install -e .` from this repo to expose the `find-emails` CLI.

## Standalone use — cheap baseline (Apify)

```python
from find_emails import find_emails

df = find_emails([
    "https://linkedin.com/in/satyanadella",
    "https://linkedin.com/in/jeffweiner08",
])
# columns: linkedin_url, email, all_emails, email_status, email_quality,
#          name, headline, company, raw
```

CLI:
```bash
find-emails people.csv --url-column linkedin_url --output emails.csv
```

## Standalone use — premium (SignalHire)

```python
from find_emails_signalhire import find_emails

df = find_emails([
    "https://linkedin.com/in/lvblack",
])
# columns: linkedin_url, email, all_emails, email_status, email_quality,
#          name, headline, company, phones, raw
# SignalHire returns full profile + multiple emails (subType: personal/work)
# + phone numbers as a bonus.
```

## Add-on use — `reveal.py` for plugging into a larger app

Use this when:

- You're integrating the email-find logic into a recruiting / outreach / CRM
  product
- Calls happen on-demand (per user click), not batch
- You want one uniform return shape regardless of which provider answered
- You want the caller to control which source(s) to try, and in what order

```python
from reveal import reveal_email

# Default — Apify only (~$0.01)
result = reveal_email("https://linkedin.com/in/lvblack")
# RevealResult(email="...", source="apify_harvestapi", cost_usd=0.01, ...)

# Escalation — caller decides when. e.g. user marked Apify result as wrong
result = reveal_email("https://linkedin.com/in/lvblack",
                      sources=["signalhire"])

# Full cascade (rare; usually let users trigger escalation per-click)
result = reveal_email("https://linkedin.com/in/lvblack",
                      sources=["apify_harvestapi", "signalhire"])

# Result shape (uniform across providers):
#   result.email       - best email or None
#   result.alt_email   - second-best email if any
#   result.phones      - list of phones (SignalHire bonus)
#   result.source      - "apify_harvestapi" | "signalhire" | "none"
#   result.cost_usd    - what this call cost
#   result.status      - "valid" / "risky" / "personal" / "work"
#   result.all_emails  - everything the provider returned
#   result.profile     - {name, headline, company} for context
#   result.found       - True if email is non-empty
```

### Drawer / CRM integration sketch

```python
# Your app's reveal endpoint:
def reveal_for_drawer(person, user):
    # Already have an email and it's not flagged wrong → return cached
    if person.email_1 and not person.email_marked_incorrect:
        return person.email_1

    # Either first reveal, or user marked previous email as wrong.
    # Default = Apify; escalate to SignalHire only after a flag.
    sources = (["signalhire"] if person.email_marked_incorrect
               else ["apify_harvestapi"])

    result = reveal_email(person.linkedin_url, sources=sources)

    if result.found:
        if person.email_marked_incorrect:
            person.email_2 = result.email           # alternate
            person.email_2_source = result.source
        else:
            person.email_1 = result.email           # primary
            person.email_source = result.source
        save(person)
        log_audit(user, person, result)
    return result
```

That's the entire integration. Cost stays predictable: $0.01 per first reveal,
$0.06 only when a user explicitly flags the previous result as wrong.

## What this replaced

```
                          BEFORE                           AFTER
──────────────────────────────────────────────────────────────────────────
  6-pass cascade with verifier      0% best-pick correct    47% (single
                                    on 2127 students,       harvestapi pass
                                    ~$30 wasted             at ~$10) +
                                                            on-demand
                                                            SignalHire for
                                                            specific reveals
```

The legacy cascade is gone. See commit `ab411ce` for the deletion.
The bake-off that picked harvestapi out of 5 candidates lives at
`scripts/actor_bakeoff.py` — useful when you ever want to compare new actors.

## Cost / accuracy notes

| | Apify harvestapi | SignalHire |
|-|-|-|
| Cost | $10 / 1000 profiles (pay-per-result) | $57 / 1000 credits ≈ $0.06/lookup |
| Hit rate (working pros) | ~70% | ~80% |
| Hit rate (students) | ~50% | ~80% |
| Returns personal email subType | partial | yes (explicit flag) |
| Returns phone numbers | no | yes (bonus on personal-email tier) |
| Returns full LinkedIn profile | yes | yes (skills, education, experience) |
| Verified flag | partial (`email_status`) | rating 0-100 + status |

Empirical scorecard from a 5-row stress test (2 covered earlier in commit
history; harvestapi vs SignalHire vs Apollo):

| Person | Truth | harvestapi | SignalHire | Apollo |
|-|-|-|-|-|
| Lucy Black | lvblack@stanford.edu | ❌ | ✅ exact | ❌ extrapolated guess |
| Naomi Wong | nwongg@berkeley.edu | ✅ | ✅ exact + alt | ✅ verified |
| Anu Kirk | anu.kirk@gmail.com | ❌ | ✅ exact + 4 phones | ✅ in personal_emails[] |
| Salina Jiang | salina.jiang@alphasights.com | ❌ | ⚠ schwab.com | ⚠ null (truth stale) |
| Ke Xu | kx@daicus.com | ❌ | ✅ exact + alt gmail | ⚠ TrusOne (newer role) |

**Net:** harvestapi 1/5, SignalHire 4/5, Apollo 2/5. SignalHire is the right
default for "find personal email." Apollo not currently wired (would be
trivial to add as a third source — same pattern).

## License

MIT.
