from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich import print
from rich.console import Console
from rich.table import Table

from find_me_email.apify_client import ApifyClient
from find_me_email.csv_io import read_people, write_results
from find_me_email.learning import recommend_cascade
from find_me_email.orchestrator import Orchestrator
from find_me_email.settings import settings
from find_me_email.validation import validate as run_validate

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

DEFAULT_CONFIG = Path("config/providers.yaml")


@app.command()
def whoami():
    """Confirm Apify token works and show remaining credits."""

    async def _run():
        async with ApifyClient() as ac:
            info = await ac.get_user_info()
        usage = info.get("plan", {})
        print(f"[green]✓[/green] authenticated as [bold]{info.get('username', '?')}[/bold]")
        print(f"plan: {usage}")
    asyncio.run(_run())


@app.command()
def estimate(
    input_csv: Path = typer.Argument(..., exists=True, readable=True),
    sample: int | None = typer.Option(None, help="Estimate against a random subset"),
    config: Path = typer.Option(DEFAULT_CONFIG, exists=True),
):
    """Print expected cost without calling any provider."""
    import yaml
    people = read_people(input_csv, sample=sample)
    cfg = yaml.safe_load(config.read_text())
    print(f"[bold]{len(people)}[/bold] people to enrich")
    table = Table("Provider", "Cost/call", "Worst-case total")
    total = 0.0
    for p in cfg["cascade"]:
        if not p.get("enabled", True):
            continue
        from find_me_email.providers import build_provider
        prov = build_provider(p["name"], p)
        wc = prov.cost_per_call_usd * len(people)
        total += wc
        table.add_row(p["name"], f"${prov.cost_per_call_usd:.4f}", f"${wc:.2f}")
    console.print(table)
    print(f"[yellow]Worst-case ALL providers run on ALL people: ${total:.2f}[/yellow]")
    print(f"Budget cap: ${settings.budget_usd:.2f}")


@app.command()
def enrich(
    input_csv: Path = typer.Argument(..., exists=True, readable=True),
    output_csv: Path = typer.Option(Path("data/output/enriched.csv")),
    sample: int | None = typer.Option(None, help="Only run on a random subset (e.g., 100 for POC)"),
    seed: int = typer.Option(42),
    config: Path = typer.Option(DEFAULT_CONFIG, exists=True),
    force: bool = typer.Option(False, help="Ignore per-row cache"),
):
    """Run the cascade and write enriched CSV."""
    people = read_people(input_csv, sample=sample, seed=seed)
    print(f"Loaded [bold]{len(people)}[/bold] people from {input_csv}")
    if sample:
        print(f"[dim](random sample, seed={seed})[/dim]")

    orch = Orchestrator(config)

    async def _run():
        return await orch.run(people, force_refresh=force)

    results = asyncio.run(_run())
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    write_results(results, output_csv)

    found = sum(1 for r in results if r.best)
    print(f"\n[green]✓[/green] wrote {output_csv} ({found}/{len(results)} with at least one candidate)")
    print(f"Total spend: [bold]${orch.spent_usd:.2f}[/bold] (budget ${orch.budget_usd:.2f})")


@app.command()
def validate(
    enriched_csv: Path = typer.Argument(..., exists=True),
    ground_truth_csv: Path = typer.Argument(..., exists=True),
    config: Path = typer.Option(DEFAULT_CONFIG, exists=True),
    out: Path = typer.Option(Path("data/output/validation_report.md")),
):
    """Compare an enriched CSV against ground-truth and print precision/recall per provider."""
    # Reload results from per-row cache to get full candidate list (CSV is lossy).
    from find_me_email.schemas import EnrichmentResult
    cache_dir = settings.cache_dir
    results: list[EnrichmentResult] = []
    for path in cache_dir.glob("*.json"):
        try:
            results.append(EnrichmentResult.model_validate_json(path.read_text()))
        except Exception:
            continue
    report = run_validate(results, ground_truth_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_markdown())
    print(report.to_markdown())
    print(f"\n[green]✓[/green] saved {out}")


@app.command()
def stats(config: Path = typer.Option(DEFAULT_CONFIG, exists=True)):
    """Show learning stats and recommended cascade reordering."""
    import yaml
    cfg = yaml.safe_load(config.read_text())
    stats_path = Path(cfg["learning"]["stats_file"])
    rec = recommend_cascade(stats_path)
    if not rec:
        print("[yellow]No stats yet. Run `enrich` first.[/yellow]")
        raise typer.Exit()
    table = Table("Provider", "Hit rate")
    for name, rate in rec:
        table.add_row(name, f"{rate:.0%}")
    console.print(table)


if __name__ == "__main__":
    app()
