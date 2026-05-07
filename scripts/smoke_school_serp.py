"""Live smoke test for ApifySchoolSerpProvider.

Runs the new provider in isolation against 3 v3 rows that had only
speculative / low-confidence guesses, and prints what it found. Intended
to be run once after the provider is wired up; not part of the cascade.
"""
from __future__ import annotations

import asyncio

from rich.console import Console

from find_me_email.providers.apify_school_serp import ApifySchoolSerpProvider
from find_me_email.schemas import Person

console = Console()

PEOPLE = [
    Person(
        row_id="20",
        name="Zixuan Yan",
        school="Columbia University",
        school_domain="columbia.edu",
    ),
    Person(
        row_id="49",
        name="Xiaojing Xing",
        school="Stanford University School of Medicine",
        school_domain="stanford.edu",
    ),
    Person(
        row_id="5",
        name="Hariprashad Ravikumar",
        school="New Mexico State University",
        school_domain="nmsu.edu",
    ),
]


async def main():
    prov = ApifySchoolSerpProvider(
        {
            "actor_id": "apify/google-search-scraper",
            "results_per_query": 10,
            "max_domains_per_person": 2,
            "fetch_top_pages": 3,
        }
    )
    console.print(f"[cyan]Running {prov.name} on {len(PEOPLE)} people...[/cyan]")
    results = await prov.enrich_batch(PEOPLE)
    for p in PEOPLE:
        cands = results.get(p.row_id, [])
        console.print(f"\n[bold]{p.name}[/bold] ({p.school_domain}) — {len(cands)} candidates")
        for c in cands:
            console.print(
                f"  {c.email}  [{c.confidence.value}]  {c.notes}  "
                f"src={(c.raw or {}).get('source_url', '?')[:70]}"
            )


if __name__ == "__main__":
    asyncio.run(main())
