from __future__ import annotations

import math
from collections import defaultdict


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return round(xs[lo], 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo), 1)


def summarize(rows: list[dict], mode: str) -> dict:
    answerable = [r for r in rows if r["answerable"]]
    no_answer = [r for r in rows if not r["answerable"]]
    out = {"questions": len(rows), "answerable": len(answerable), "no_evidence": len(no_answer)}
    for k in (1, 3, 5):
        out[f"hit@{k}"] = (sum(1 for r in answerable if r.get("rank") and r["rank"] <= k)
                            / len(answerable) if answerable else None)
    out["mrr"] = (sum(1 / r["rank"] for r in answerable if r.get("rank")) / len(answerable)
                  if answerable else None)
    if mode == "pipeline":
        out["answerable_gate_recall"] = (
            sum(1 for r in answerable if r.get("has_evidence")) / len(answerable)
            if answerable else None)
        out["no_evidence_recall"] = (
            sum(1 for r in no_answer if not r.get("has_evidence")) / len(no_answer)
            if no_answer else None)
    times = [float(r["ms"]) for r in rows if r.get("ms") is not None]
    out["latency_ms"] = {"p50": percentile(times, 0.50), "p95": percentile(times, 0.95)}
    return out


def by_tag(rows: list[dict], mode: str) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        for tag in row.get("tags", []):
            grouped[tag].append(row)
    return {tag: summarize(items, mode) for tag, items in sorted(grouped.items())}
