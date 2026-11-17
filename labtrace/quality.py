from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .contracts import Channel, LabError, Observation, Protocol
from .measure import within_range


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str

    def document(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


def assess(measurement: dict[str, Any], channel: Channel) -> Decision:
    if not within_range(measurement["value"], channel):
        return Decision("quarantined", "out_of_range")
    if measurement["control"] != "sample":
        return Decision("control", measurement["control"])
    if measurement["sample"].lower().startswith("unknown"):
        return Decision("quarantined", "unknown_sample")
    return Decision("eligible", "")


def page_decisions(observations: list[Observation]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in observations:
        result[item.status] = result.get(item.status, 0) + 1
    return result


def batch_state(pages: list[dict[str, Any]]) -> str:
    if not pages:
        return "pending"
    states = [item["state"] for item in pages]
    if all(state == "committed" for state in states):
        return "complete"
    if any(state == "failed" for state in states):
        return "partial_failed"
    if any(state == "committed" for state in states):
        return "partial"
    return "pending"


def exposure(status: str, withdrawn: bool) -> bool:
    return status == "eligible" and not withdrawn


def quality_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"eligible": 0, "quarantined": 0, "control": 0}
    for row in rows:
        status = row["status"]
        counts[status] = counts.get(status, 0) + 1
    return counts


def validate_page_measurements(rows: list[Observation]) -> None:
    keys: set[tuple[str, str]] = set()
    for row in rows:
        key = (row.identity, row.measured_at)
        if key in keys:
            raise LabError("duplicate_measurement", row.identity)
        keys.add(key)


def explain_exclusion(row: dict[str, Any]) -> str:
    if row.get("withdrawn"):
        return "source_withdrawn"
    if row["status"] == "control":
        return "control_measurement"
    if row["status"] == "quarantined":
        return row["reason"] or "quality_quarantine"
    return ""


def protocol_health(protocol: Protocol) -> dict[str, Any]:
    return {"key": protocol.key, "revision": protocol.revision,
            "channels": len(protocol.channels),
            "all_ranges_valid": all(Decimal(ch.minimum) < Decimal(ch.maximum) for ch in protocol.channels),
            "page_limit": protocol.page_limit}
