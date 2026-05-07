"""Cascade orchestrator with budget guard, per-row caching, and multi-pass support.

Two YAML shapes are supported. `cascade:` is a flat provider list (one pass).
`passes:` is an ordered list of named mini-cascades, run sequentially on rows
that aren't yet HIGH+verified, with a CSV checkpoint after each pass.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import yaml
from rich.console import Console
from rich.table import Table

from find_me_email.providers import build_provider
from find_me_email.providers.base import EnrichmentProvider
from find_me_email.schemas import EmailCandidate, EnrichmentResult, Person
from find_me_email.settings import settings
from find_me_email.verifier import build_verifier

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

        if "passes" in self.cfg and self.cfg["passes"]:
            self.mode = "passes"
            self.passes: list[dict[str, Any]] = self.cfg["passes"]
        else:
            self.mode = "cascade"
            self.passes = []
            self._cascade_cfg: list[dict[str, Any]] = [
                p for p in self.cfg.get("cascade", []) if p.get("enabled", True)
            ]

        self.cache_dir = settings.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # CLI sets this to dump a CSV after each pass without coupling the
        # orchestrator to csv_io.
        self.checkpoint_writer: Callable[[int, str, list[EnrichmentResult]], None] | None = None
        self.coverage_per_pass: list[dict[str, Any]] = []

    @property
    def providers(self) -> list[EnrichmentProvider]:
        """Flat list of all enabled providers, used by `estimate` / `benchmark`."""
        if self.mode == "passes":
            cfgs = [
                prov
                for ps in self.passes
                for prov in ps.get("providers", [])
                if prov.get("enabled", True)
            ]
        else:
            cfgs = self._cascade_cfg
        return [build_provider(p["name"], p) for p in cfgs]

    # ---------- per-row result cache (resumable) ----------

    def _cache_path(self, row_id: str) -> Path:
        safe = "".join(c if c.isalnum() else "_" for c in row_id)[:80]
        return self.cache_dir / f"{safe}.json"

    def _load_cached(self, person: Person) -> EnrichmentResult | None:
        p = self._cache_path(person.row_id)
        if not p.exists():
            return None
        try:
            return EnrichmentResult.model_validate_json(p.read_text())
        except Exception:
            return None

    def _save_cached(self, result: EnrichmentResult) -> None:
        self._cache_path(result.person.row_id).write_text(result.model_dump_json(indent=2))

    def _hydrate_accumulator(
        self, people: list[Person], force_refresh: bool
    ) -> dict[str, EnrichmentResult]:
        if force_refresh:
            return {}
        out: dict[str, EnrichmentResult] = {}
        for person in people:
            cached = self._load_cached(person)
            if cached:
                out[person.row_id] = cached
        return out

    # ---------- main ----------

    async def run(
        self, people: list[Person], force_refresh: bool = False
    ) -> list[EnrichmentResult]:
        if self.mode == "passes":
            results = await self._run_passes(people, force_refresh=force_refresh)
        else:
            results = await self._run_cascade_legacy(people, force_refresh=force_refresh)

        verifier = build_verifier(self.cfg.get("verifier") or {})
        if verifier is not None:
            unverified_count = sum(
                1 for r in results for c in r.candidates if c.email and not c.verified
            )
            results = await verifier.verify(results)
            for r in results:
                self._save_cached(r)
            self.spent_usd += verifier.cost_per_call_usd * unverified_count
            verified_count = sum(
                1 for r in results if any(c.verified for c in r.candidates if c.email)
            )
            self.coverage_per_pass.append(
                {
                    "pass": f"verifier ({verifier.name})",
                    "any_candidate": sum(1 for r in results if r.has_any),
                    "medium_or_better": sum(1 for r in results if r.has_medium_or_better),
                    "strong": verified_count,
                    "total": len(results),
                }
            )
        return results

    # ---------- multi-pass mode ----------

    async def _run_passes(
        self, people: list[Person], force_refresh: bool
    ) -> list[EnrichmentResult]:
        accumulator = self._hydrate_accumulator(people, force_refresh)
        provider_stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"attempted": 0, "hits": 0}
        )

        for pass_idx, pass_cfg in enumerate(self.passes):
            pass_name: str = pass_cfg.get("name") or f"pass_{pass_idx + 1}"
            provider_cfgs = [
                p for p in pass_cfg.get("providers", []) if p.get("enabled", True)
            ]
            if not provider_cfgs:
                console.print(f"[dim]{pass_name}: no enabled providers, skipping[/dim]")
                continue
            pass_providers = [build_provider(p["name"], p) for p in provider_cfgs]

            pending = [p for p in people if not self._is_strong(accumulator, p)]
            console.print(
                f"\n[bold cyan]══ Pass {pass_idx + 1}/{len(self.passes)}: "
                f"{pass_name}[/bold cyan] ({len(pending)} pending, "
                f"{len(people) - len(pending)} already strong)"
            )
            if not pending:
                continue

            await self._run_provider_chain(
                pending, pass_providers, accumulator, provider_stats
            )

            results_now = self._materialize(people, accumulator)
            self.coverage_per_pass.append(
                {
                    "pass": pass_name,
                    "any_candidate": sum(1 for r in results_now if r.has_any),
                    "medium_or_better": sum(
                        1 for r in results_now if r.has_medium_or_better
                    ),
                    "strong": sum(1 for r in results_now if r.is_strong),
                    "total": len(people),
                }
            )

            if self.checkpoint_writer is not None:
                try:
                    self.checkpoint_writer(pass_idx + 1, pass_name, results_now)
                except Exception as e:
                    console.print(f"[yellow]checkpoint write failed: {e}[/yellow]")

        self._write_stats(provider_stats)
        return self._materialize(people, accumulator)

    @staticmethod
    def _is_strong(
        accumulator: dict[str, EnrichmentResult], person: Person
    ) -> bool:
        r = accumulator.get(person.row_id)
        return r is not None and r.is_strong

    @staticmethod
    def _materialize(
        people: list[Person], accumulator: dict[str, EnrichmentResult]
    ) -> list[EnrichmentResult]:
        out: list[EnrichmentResult] = []
        for p in people:
            r = accumulator.get(p.row_id)
            out.append(r if r is not None else EnrichmentResult(person=p))
        return out

    # ---------- legacy single-cascade mode ----------

    async def _run_cascade_legacy(
        self, people: list[Person], force_refresh: bool
    ) -> list[EnrichmentResult]:
        accumulator = self._hydrate_accumulator(people, force_refresh)
        providers = [
            build_provider(p["name"], p) for p in self._cascade_cfg
        ]
        pending = [p for p in people if not self._is_strong(accumulator, p)]

        provider_stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"attempted": 0, "hits": 0}
        )
        await self._run_provider_chain(pending, providers, accumulator, provider_stats)
        self._write_stats(provider_stats)
        return self._materialize(people, accumulator)

    # ---------- provider chain (shared by both modes) ----------

    async def _run_provider_chain(
        self,
        pending_input: list[Person],
        providers: list[EnrichmentProvider],
        accumulator: dict[str, EnrichmentResult],
        provider_stats: dict[str, dict[str, int]],
    ) -> None:
        """Cascade `pending_input` through `providers`. Mutates accumulator + stats."""
        if not providers:
            return

        pending: dict[str, list[Person]] = {p.name: [] for p in providers}
        # Skip providers already attempted for a row from a prior run/pass.
        for person in pending_input:
            existing = accumulator.get(person.row_id)
            attempted = set(existing.providers_attempted) if existing else set()
            for prov in providers:
                if prov.name not in attempted:
                    pending[prov.name].append(person)
                    break

        for idx, provider in enumerate(providers):
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
                if idx + 1 < len(providers):
                    pending[providers[idx + 1].name].extend(batch)
                continue

            for person in batch:
                cands = batch_results.get(person.row_id, [])
                provider_stats[provider.name]["attempted"] += 1
                self.spent_usd += sum(c.cost_usd for c in cands) or (
                    provider.cost_per_call_usd if cands else 0.0
                )

                existing = accumulator.get(person.row_id)
                result = existing or EnrichmentResult(person=person)
                changed = bool(cands) or provider.name not in result.providers_attempted
                result.candidates.extend(cands)
                if provider.name not in result.providers_attempted:
                    result.providers_attempted.append(provider.name)
                result.total_cost_usd = round(
                    result.total_cost_usd + sum(c.cost_usd for c in cands), 6
                )
                accumulator[person.row_id] = result

                if result.is_strong:
                    provider_stats[provider.name]["hits"] += 1
                elif idx + 1 < len(providers):
                    pending[providers[idx + 1].name].append(person)

                if changed:
                    self._save_cached(result)

    # ---------- stats ----------

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

    def coverage_table(self) -> Table:
        t = Table(
            "Pass",
            "Strong (HIGH+verified)",
            "Med+ unverified",
            "Any candidate",
            "Total",
            title="Coverage by pass",
        )
        for row in self.coverage_per_pass:
            total = max(row["total"], 1)
            t.add_row(
                row["pass"],
                f"{row['strong']}/{row['total']} ({row['strong'] / total:.0%})",
                f"{row['medium_or_better']}/{row['total']} ({row['medium_or_better'] / total:.0%})",
                f"{row['any_candidate']}/{row['total']} ({row['any_candidate'] / total:.0%})",
                str(row["total"]),
            )
        return t
