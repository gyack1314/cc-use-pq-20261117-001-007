from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import LabError, canonical, digest_bytes
from .quality import quality_summary
from .resolve import conflict_count, reconcile_all, usable_count
from .store import Store


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "instrument", "batch", "ordinal", "row_number", "plate", "sample",
            "well", "channel", "value", "unit", "measured_at", "identity", "status",
            "reason", "raw", "observed_at", "manifest_digest", "withdrawn",
            "withdrawn_at", "withdrawal_reason")
    return {key: row[key] for key in keys if key in row}


class Delivery:
    def __init__(self, store: Store):
        self.store = store

    def _tolerances(self) -> dict[str, Decimal]:
        result: dict[str, Decimal] = {}
        for document in self.store.protocols():
            for channel in document["channels"]:
                tolerance = Decimal(channel["tolerance"])
                if channel["key"] in result:
                    result[channel["key"]] = min(result[channel["key"]], tolerance)
                else:
                    result[channel["key"]] = tolerance
        return result

    def current(self, *, include_excluded: bool = False) -> dict[str, Any]:
        rows = self.store.observations()
        resolved = reconcile_all(rows, tolerance_by_channel=self._tolerances())
        if not include_excluded:
            resolved = [row for row in resolved if row["state"] != "excluded"]
        return {"records": resolved, "usable": usable_count(resolved),
                "conflicts": conflict_count(resolved),
                "quality": quality_summary(rows)}

    def history(self, *, instrument: str | None = None,
                identity: str | None = None) -> dict[str, Any]:
        return {"observations": [public_row(item) for item in
                                 self.store.observations(instrument=instrument, identity=identity)],
                "events": self.store.events(instrument)}

    def trace(self, identity: str) -> dict[str, Any]:
        rows = self.store.observations(identity=identity)
        resolved = reconcile_all(rows, tolerance_by_channel=self._tolerances())
        return {"identity": identity, "observations": [public_row(item) for item in rows],
                "resolution": resolved[0] if resolved else None}

    def summary(self) -> dict[str, Any]:
        current = self.current(include_excluded=True)
        batches = self.store.batches()
        return {"batch_count": len(batches), "page_count": sum(len(item["pages"]) for item in batches),
                "observation_count": len(self.store.observations()),
                "usable": current["usable"], "conflicts": current["conflicts"],
                "quality": current["quality"],
                "batch_states": {item["instrument"] + "/" + item["batch"]: item["state"] for item in batches}}

    def snapshot_document(self, *, include_excluded: bool = False) -> dict[str, Any]:
        return {"schema_version": 1, "current": self.current(include_excluded=include_excluded),
                "summary": self.summary()}

    def write_snapshot(self, destination: str | Path, *, include_excluded: bool = False) -> dict[str, Any]:
        path = Path(destination)
        if path.exists():
            raise LabError("snapshot_exists", str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        document = self.snapshot_document(include_excluded=include_excluded)
        payload = (canonical(document) + "\n").encode("utf-8")
        with path.open("xb") as handle:
            handle.write(payload)
        sha = digest_bytes(payload)
        record = self.store.record_snapshot(str(path), sha, len(document["current"]["records"]))
        return {**record, "summary": document["summary"]}


def verify_snapshot(path: str | Path, expected_sha256: str) -> dict[str, Any]:
    candidate = Path(path)
    try:
        payload = candidate.read_bytes()
    except FileNotFoundError:
        raise LabError("snapshot_missing", str(candidate)) from None
    actual = digest_bytes(payload)
    if actual != expected_sha256:
        raise LabError("snapshot_digest_mismatch", str(candidate))
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        raise LabError("snapshot_invalid_json", str(candidate)) from None
    return {"sha256": actual, "record_count": len(document["current"]["records"]),
            "summary": document["summary"]}
