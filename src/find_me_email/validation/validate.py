"""Validate predicted emails against a ground-truth CSV.

Ground-truth CSV must have columns: row_id, true_email
Output: per-provider precision (matched / predicted), recall (matched / total),
overall hit rate, and cost-per-correct-email.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from find_me_email.schemas import EnrichmentResult


class ProviderMetrics(BaseModel):
    provider: str
    predicted: int = 0
    correct: int = 0
    cost_usd: float = 0.0

    @property
    def precision(self) -> float:
        return self.correct / self.predicted if self.predicted else 0.0

    @property
    def cost_per_correct(self) -> float:
        return self.cost_usd / self.correct if self.correct else float("inf")


class ValidationReport(BaseModel):
    total_people: int
    has_ground_truth: int
    overall_hits: int
    by_provider: list[ProviderMetrics]
    total_cost_usd: float

    @property
    def overall_recall(self) -> float:
        return self.overall_hits / self.has_ground_truth if self.has_ground_truth else 0.0

    def to_markdown(self) -> str:
        lines = [
            "# Validation report",
            "",
            f"- Total people: **{self.total_people}**",
            f"- With ground-truth email: **{self.has_ground_truth}**",
            f"- Overall hits (any provider matched): **{self.overall_hits}** "
            f"({self.overall_recall:.0%} recall)",
            f"- Total cost: **${self.total_cost_usd:.2f}**",
            "",
            "## Per-provider",
            "",
            "| Provider | Predicted | Correct | Precision | Cost | Cost/correct |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for m in self.by_provider:
            cpc = f"${m.cost_per_correct:.4f}" if m.correct else "—"
            lines.append(
                f"| {m.provider} | {m.predicted} | {m.correct} | "
                f"{m.precision:.0%} | ${m.cost_usd:.2f} | {cpc} |"
            )
        return "\n".join(lines)


def validate(results: list[EnrichmentResult], ground_truth_csv: Path) -> ValidationReport:
    gt = pd.read_csv(ground_truth_csv)
    truth: dict[str, str] = {
        str(row["row_id"]): str(row["true_email"]).strip().lower()
        for _, row in gt.iterrows()
        if pd.notna(row.get("true_email"))
    }

    metrics: dict[str, ProviderMetrics] = defaultdict(lambda: ProviderMetrics(provider="?"))
    overall_hits = 0
    total_cost = 0.0

    for r in results:
        true_email = truth.get(r.person.row_id)
        person_hit = False
        for cand in r.candidates:
            m = metrics.setdefault(cand.source_provider, ProviderMetrics(provider=cand.source_provider))
            m.predicted += 1
            m.cost_usd += cand.cost_usd
            total_cost += cand.cost_usd
            if true_email and cand.email.lower() == true_email:
                m.correct += 1
                person_hit = True
        if true_email and person_hit:
            overall_hits += 1

    return ValidationReport(
        total_people=len(results),
        has_ground_truth=len(truth),
        overall_hits=overall_hits,
        by_provider=sorted(metrics.values(), key=lambda x: -x.correct),
        total_cost_usd=round(total_cost, 4),
    )
