"""Quick smoke tests that don't hit any real APIs."""
from pathlib import Path

import pandas as pd

from find_me_email.college_domains import resolve_domain
from find_me_email.csv_io import read_people, write_results
from find_me_email.providers.college_pattern_guess import CollegePatternGuessProvider
from find_me_email.schemas import Confidence, EnrichmentResult, Person


def test_domain_resolver_known():
    assert resolve_domain("Stanford University") == "stanford.edu"
    assert resolve_domain("MIT") == "mit.edu"
    assert resolve_domain("San Jose State University") == "sjsu.edu"


def test_domain_resolver_passthrough_domain():
    assert resolve_domain("berkeley.edu") == "berkeley.edu"


def test_domain_resolver_heuristic():
    # No curated entry, but should produce a plausible .edu guess
    assert resolve_domain("Foo Bar Tech University").endswith(".edu")


async def test_pattern_guess_generates_candidates():
    p = Person(row_id="1", name="Jane Doe", school="Stanford University")
    p.school_domain = resolve_domain(p.school)
    guesser = CollegePatternGuessProvider()
    cands = await guesser.enrich(p)
    emails = {c.email for c in cands}
    assert "jane.doe@stanford.edu" in emails
    assert all(c.confidence == Confidence.SPECULATIVE for c in cands)
    assert all("PATTERN GUESS" in c.notes for c in cands)


def test_csv_roundtrip(tmp_path: Path):
    src = tmp_path / "in.csv"
    pd.DataFrame(
        [
            {"Name": "Jane Doe", "LinkedIn URL": "https://linkedin.com/in/janedoe", "College": "Stanford"},
            {"Name": "John Roe", "LinkedIn URL": "https://linkedin.com/in/johnroe", "College": "MIT"},
        ]
    ).to_csv(src, index=False)
    people = read_people(src)
    assert len(people) == 2
    assert people[0].school_domain == "stanford.edu"
    assert people[1].school_domain == "mit.edu"

    results = [EnrichmentResult(person=p) for p in people]
    out = tmp_path / "out.csv"
    write_results(results, out)
    df = pd.read_csv(out)
    assert "best_email" in df.columns
    assert len(df) == 2
