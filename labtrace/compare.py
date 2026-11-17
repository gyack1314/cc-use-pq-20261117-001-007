from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from .contracts import LabError
from .release import Delivery
from .store import Store


def _identity_map(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        identity = record.get("identity")
        if not isinstance(identity, str) or identity in result:
            raise LabError("invalid_comparison_record", str(identity))
        result[identity] = record
    return result


def _winner_key(record: dict[str, Any] | None) -> tuple[str, str, str] | None:
    if record is None:
        return None
    return (str(record.get("instrument")), str(record.get("value")),
            str(record.get("unit")))


def compare_views(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    left = _identity_map(before.get("records", []))
    right = _identity_map(after.get("records", []))
    added = []
    removed = []
    changed = []
    for identity in sorted(set(left) | set(right)):
        prior = left.get(identity)
        later = right.get(identity)
        if prior is None:
            added.append({"identity": identity, "state": later["state"],
                          "winner": later.get("winner")})
        elif later is None:
            removed.append({"identity": identity, "state": prior["state"],
                            "winner": prior.get("winner")})
        elif prior["state"] != later["state"] or _winner_key(prior.get("winner")) != _winner_key(later.get("winner")):
            changed.append({"identity": identity,
                            "before_state": prior["state"], "after_state": later["state"],
                            "before_winner": prior.get("winner"), "after_winner": later.get("winner")})
    return {"added": added, "removed": removed, "changed": changed,
            "before_usable": before.get("usable", 0), "after_usable": after.get("usable", 0),
            "before_conflicts": before.get("conflicts", 0), "after_conflicts": after.get("conflicts", 0)}


class Comparator:
    def __init__(self, store: Store):
        self.store = store

    def snapshot_to_current(self, snapshot_id: int) -> dict[str, Any]:
        from pathlib import Path
        import json
        from .contracts import digest_bytes

        row = self.store.connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise LabError("unknown_snapshot", str(snapshot_id))
        path = Path(row["path"])
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            raise LabError("snapshot_missing", str(path)) from None
        if digest_bytes(payload) != row["sha256"]:
            raise LabError("snapshot_digest_mismatch", str(path))
        try:
            before = json.loads(payload)["current"]
        except (ValueError, TypeError, KeyError):
            raise LabError("snapshot_invalid", str(path)) from None
        after = Delivery(self.store).current()
        result = compare_views(before, after)
        result["snapshot_id"] = snapshot_id
        result["snapshot_sha256"] = row["sha256"]
        return result

    def batch_to_current(self, instrument: str, batch: str) -> dict[str, Any]:
        stored = self.store.batch(instrument, batch)
        rows = [item for item in self.store.observations(instrument=instrument)
                if item["batch"] == batch]
        current = Delivery(self.store).current(include_excluded=True)
        by_identity = {record["identity"]: record for record in current["records"]}
        affected = []
        for identity in sorted({item["identity"] for item in rows}):
            record = by_identity.get(identity)
            if record:
                affected.append({"identity": identity, "state": record["state"],
                                 "winner": record["winner"],
                                 "batch_observations": sum(item["identity"] == identity for item in rows)})
        return {"instrument": instrument, "batch": batch,
                "batch_state": stored["state"], "manifest_digest": stored["digest"],
                "affected": affected}

    def source_contribution(self, instrument: str) -> dict[str, Any]:
        state = self.store.instrument_state(instrument)
        current = Delivery(self.store).current(include_excluded=True)
        selected = []
        challenged = []
        excluded = []
        for item in current["records"]:
            winner = item["winner"]
            if winner and winner["instrument"] == instrument:
                selected.append(item["identity"])
            elif any(candidate["instrument"] == instrument for candidate in item["candidates"]):
                challenged.append(item["identity"])
            if any(candidate["instrument"] == instrument for candidate in item["excluded"]):
                excluded.append(item["identity"])
        return {"instrument": instrument, "withdrawn": bool(state["withdrawn_at"]),
                "selected": sorted(selected), "challenged": sorted(challenged),
                "excluded": sorted(excluded),
                "selected_count": len(selected), "challenged_count": len(challenged),
                "excluded_count": len(excluded)}
