from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Iterable

from .measure import stable_numeric_order
from .quality import explain_exclusion, exposure


def latest_per_instrument(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["instrument"]
        prior = latest.get(key)
        if prior is None or (row["measured_at"], row["id"]) > (prior["measured_at"], prior["id"]):
            latest[key] = row
    return list(latest.values())


def _candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "instrument": row["instrument"],
            "batch": row["batch"], "ordinal": row["ordinal"],
            "value": row["value"], "unit": row["unit"],
            "measured_at": row["measured_at"], "status": row["status"],
            "manifest_digest": row["manifest_digest"]}


def reconcile_group(rows: list[dict[str, Any]], *, tolerance: Decimal) -> dict[str, Any]:
    identity = rows[0]["identity"]
    active = [row for row in latest_per_instrument(rows)
              if exposure(row["status"], row["withdrawn"])]
    excluded = [{"id": row["id"], "instrument": row["instrument"],
                 "reason": explain_exclusion(row)} for row in rows
                if not exposure(row["status"], row["withdrawn"])]
    if not active:
        return {"identity": identity, "state": "excluded", "winner": None,
                "candidates": [], "excluded": excluded}
    values = [Decimal(row["value"]) for row in active]
    if max(values) - min(values) > tolerance:
        return {"identity": identity, "state": "conflict", "winner": None,
                "candidates": [_candidate(row) for row in stable_numeric_order(active)],
                "excluded": excluded}
    preferred = sorted(active, key=lambda row: (row["trust"], row["measured_at"], row["id"]), reverse=True)[0]
    state = "confirmed" if len(active) > 1 else "single_source"
    return {"identity": identity, "state": state, "winner": _candidate(preferred),
            "candidates": [_candidate(row) for row in stable_numeric_order(active)],
            "excluded": excluded}


def reconcile_all(rows: list[dict[str, Any]], *,
                  tolerance_by_channel: dict[str, Decimal]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["identity"]].append(row)
    result = []
    for identity, group in sorted(grouped.items()):
        tolerance = tolerance_by_channel.get(group[0]["channel"], Decimal("0"))
        result.append(reconcile_group(group, tolerance=tolerance))
    return result


def conflict_count(results: Iterable[dict[str, Any]]) -> int:
    return sum(result["state"] == "conflict" for result in results)


def usable_count(results: Iterable[dict[str, Any]]) -> int:
    return sum(result["winner"] is not None for result in results)


def candidate_ids(result: dict[str, Any]) -> set[int]:
    return {item["id"] for item in result["candidates"]}


def explain_result(result: dict[str, Any]) -> str:
    if result["state"] == "conflict":
        return "eligible instruments disagree beyond channel tolerance"
    if result["state"] == "excluded":
        return "no eligible non-withdrawn measurement"
    if result["state"] == "confirmed":
        return "independent instruments agree within channel tolerance"
    return "one eligible instrument supplied this measurement"
