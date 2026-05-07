"""Cascade orchestrator. Runs providers in configured order with budget + resumability."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

from find_me_email.providers import build_provider
from find_me_email.schemas import Confidence, EmailCandidate, EnrichmentResult, Person
from find_me_email.settings import settings

console = Console()


class BudgetExceeded(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, config_path: Path):
        with config_path.open() as f:
            self.cfg = yaml.safe_load(f)
        self.budget_usd: float = float(self.cfg.get("budget", {}).get("usd", settings.budget_usd))
        self.stop_on_overrun: bool = bool(self.cfg.get("budget", {}).get("stop_on_overrun", True))
        self.spent_usd: float = 0.0
        self.providers = [
            build_provider(p["name"], p) for p in self.cfg["cascade"] if p.get("enabled", True)
        ]
        self.cache_dir = settings.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---------- per-row result cache (resumable) ----------

    def _cache_path(self, row_id: str) -> Path:
        safe = "".join(c if c.isalnum() else "_" for c in row_id)[:80]
        return self.cache_dir / f"{safe}.json"

    def _load_cached(self, person: Person) -> EnrichmentResult | None:
        p = self._cache_path(person.row_id)
        if p.exists():
            try:
                return EnrichmentResult.model_validate_json(p.read_text())
            except Exception:
                return None
        return None

    def _save_cached(self, result: EnrichmentResult) -> None:
        self._cache_path(result.person.row_id).write_text(result.model_dump_json(indent=2))

    # ---------- main ----------

    async def run(self, people: list[Person], force_refresh: bool = False) -> list[EnrichmentResult]:
        results: list[EnrichmentResult] = []
        # Bucket people by which provider they need next.
        pending: dict[str, list[Person]] = {p.name: [] for p in self.providers}
        cached_results: dict[str, EnrichmentResult] = {}

        for person in people:
            if not force_refresh:
                cached = self._load_cached(person)
                if cached:
                    cached_results[person.row_id] = cached
                    continue
            pending[self.providers[0].name].append(person)

        provider_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"attempted": 0, "hits": 0})

        for idx, provider in enumerate(self.providers):
            batch = pending.get(provider.name, [])
            if not batch:
                continue

            est_cost = provider.cost_per_call_usd * len(batch)
            if self.spent_usd + est_cost > self.budget_usd and self.stop_on_overrun:
                console.print(
                    f"[yellow]Budget guard: skipping {provider.name} "
                    f"(would push spend past ${self.budget_usd}). "
                    f"Already spent ${self.spent_usd:.2f}, batch est ${est_cost:.2f}.[/yellow]"
                )
                continue

            console.print(
                f"[cyan]→ {provider.name}[/cyan] on {len(batch)} people "
                f"(est ${est_cost:.2f}, budget left ${self.budget_usd - self.spent_usd:.2f})"
            )

            try:
                batch_results = await provider.enrich_batch(batch)
            except Exception as e:
                console.print(f"[red]{provider.name} failed: {e}[/red]")
                # don't lose these people — push to next provider
                if idx + 1 < len(self.providers):
                    pending[self.providers[idx + 1].name].extend(batch)
                continue

            for person in batch:
                cands = batch_results.get(person.row_id, [])
                provider_stats[provider.name]["attempted"] += 1
                self.spent_usd += sum(c.cost_usd for c in cands) or (
                    provider.cost_per_call_usd if cands else 0.0
                )

                existing = cached_results.get(person.row_id)
                result = existing or EnrichmentResult(person=person)
                result.candidates.extend(cands)
                result.providers_attempted.append(provider.name)
                result.total_cost_usd = round(
                    result.total_cost_usd + sum(c.cost_usd for c in cands), 6
                )

                # Only short-circuit on a strong signal: HIGH confidence AND verified.
                # An unverified DB match (often a work email) shouldn't block the
                # pattern guesser from also producing a school-domain candidate.
                hit_high = any(
                    c.confidence == Confidence.HIGH and c.verified for c in cands
                )
                if hit_high:
                    provider_stats[provider.name]["hits"] += 1
                    cached_results[person.row_id] = result
                    self._save_cached(result)
                else:
                    # cascade further
                    if idx + 1 < len(self.providers):
                        pending[self.providers[idx + 1].name].append(person)
                    cached_results[person.row_id] = result
                    self._save_cached(result)

        results = list(cached_results.values())
        self._write_stats(provider_stats)
        return results

    def _write_stats(self, stats: dict[str, dict[str, int]]) -> None:
        path = Path(self.cfg["learning"]["stats_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if path.exists():
            existing = json.loads(path.read_text())
        for k, v in stats.items():
            cur = existing.setdefault(k, {"attempted": 0, "hits": 0})
            cur["attempted"] += v["attempted"]
            cur["hits"] += v["hits"]
        path.write_text(json.dumps(existing, indent=2))
