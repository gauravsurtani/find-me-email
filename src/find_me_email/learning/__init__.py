"""Learning module: aggregates per-provider stats over time and recommends a cascade order.

This is intentionally simple v1: pure success-rate weighting. Future work could
segment by archetype (school type, country, role) and pick a per-segment cascade.
"""
from __future__ import annotations

import json
from pathlib import Path


def recommend_cascade(stats_path: Path) -> list[tuple[str, float]]:
    """Return providers sorted by hit rate, descending. Cold-start = empty list."""
    if not stats_path.exists():
        return []
    data = json.loads(stats_path.read_text())
    scored = []
    for name, s in data.items():
        attempted = s.get("attempted", 0)
        hits = s.get("hits", 0)
        rate = hits / attempted if attempted else 0.0
        scored.append((name, rate))
    return sorted(scored, key=lambda x: -x[1])
