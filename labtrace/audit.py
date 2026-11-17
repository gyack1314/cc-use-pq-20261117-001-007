from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .contracts import canonical, digest_bytes, digest_document
from .store import Store


def finding(code: str, entity: str, detail: str) -> dict[str, str]:
    return {"code": code, "entity": entity, "detail": detail}


class Auditor:
    def __init__(self, store: Store):
        self.store = store

    def protocol_integrity(self) -> list[dict[str, str]]:
        problems = []
        rows = self.store.connection.execute("SELECT key,revision,document,digest FROM protocols").fetchall()
        for row in rows:
            entity = f"{row['key']}:{row['revision']}"
            try:
                document = json.loads(row["document"])
            except json.JSONDecodeError:
                problems.append(finding("protocol_json_invalid", entity, "saved declaration unreadable"))
                continue
            if digest_document(document) != row["digest"]:
                problems.append(finding("protocol_digest_mismatch", entity, "declaration changed"))
            if document.get("key") != row["key"] or document.get("revision") != row["revision"]:
                problems.append(finding("protocol_identity_mismatch", entity, "indexed identity differs"))
        return problems

    def instrument_integrity(self) -> list[dict[str, str]]:
        problems = []
        rows = self.store.connection.execute("SELECT key,document,digest,protocol_key,protocol_revision FROM instruments").fetchall()
        for row in rows:
            entity = row["key"]
            try:
                document = json.loads(row["document"])
            except json.JSONDecodeError:
                problems.append(finding("instrument_json_invalid", entity, "saved declaration unreadable"))
                continue
            if digest_document(document) != row["digest"]:
                problems.append(finding("instrument_digest_mismatch", entity, "declaration changed"))
            if (document.get("key"), document.get("protocol_key"), document.get("protocol_revision")) != (
                    row["key"], row["protocol_key"], row["protocol_revision"]):
                problems.append(finding("instrument_identity_mismatch", entity, "indexed identity differs"))
        return problems

    def batch_integrity(self) -> list[dict[str, str]]:
        problems = []
        for batch in self.store.batches():
            entity = batch["instrument"] + "/" + batch["batch"]
            if digest_document(batch["manifest"]) != batch["digest"]:
                problems.append(finding("manifest_digest_mismatch", entity, "manifest changed"))
            pages = batch["pages"]
            declared = {item["ordinal"]: item for item in batch["manifest"]["pages"]}
            if set(declared) != {page["ordinal"] for page in pages}:
                problems.append(finding("page_set_mismatch", entity, "page cursor set differs from manifest"))
            for page in pages:
                expected = declared.get(page["ordinal"])
                page_entity = entity + "/" + str(page["ordinal"])
                if expected and (page["path"], page["format"], page["expected_sha256"]) != (
                        expected["path"], expected["format"], expected["sha256"]):
                    problems.append(finding("page_contract_mismatch", page_entity, "page declaration differs"))
                actual = self.store.connection.execute(
                    "SELECT COUNT(*) AS n FROM observations WHERE instrument=? AND batch=? AND ordinal=?",
                    (batch["instrument"], batch["batch"], page["ordinal"])).fetchone()["n"]
                if actual != page["row_count"]:
                    problems.append(finding("page_row_count_mismatch", page_entity,
                                            f"cursor={page['row_count']} observations={actual}"))
                if page["state"] != "committed" and actual:
                    problems.append(finding("uncommitted_observations", page_entity, str(actual)))
            expected_state = self._expected_state(pages)
            if batch["state"] != expected_state:
                problems.append(finding("batch_state_mismatch", entity,
                                        f"stored={batch['state']} derived={expected_state}"))
        return problems

    @staticmethod
    def _expected_state(pages: list[dict[str, Any]]) -> str:
        states = [page["state"] for page in pages]
        if not states:
            return "pending"
        if all(item == "committed" for item in states):
            return "complete"
        if "failed" in states:
            return "partial_failed"
        if "committed" in states:
            return "partial"
        return "pending"

    def observation_integrity(self) -> list[dict[str, str]]:
        problems = []
        rows = self.store.observations()
        for row in rows:
            entity = str(row["id"])
            expected = "|".join((row["plate"], row["sample"], row["well"],
                                 row["channel"], str(row["raw"].get("replicate", "primary"))))
            if row["identity"] != expected:
                problems.append(finding("observation_identity_mismatch", entity,
                                        "identity does not match saved evidence"))
            if row["status"] not in {"eligible", "quarantined", "control"}:
                problems.append(finding("unknown_quality_state", entity, row["status"]))
            if row["status"] == "eligible" and row["reason"]:
                problems.append(finding("eligible_reason_present", entity, row["reason"]))
        return problems

    def snapshot_integrity(self) -> list[dict[str, str]]:
        problems = []
        for snapshot in self.store.snapshots():
            entity = str(snapshot["id"])
            path = Path(snapshot["path"])
            if not path.is_file():
                problems.append(finding("snapshot_missing", entity, snapshot["path"]))
                continue
            data = path.read_bytes()
            if digest_bytes(data) != snapshot["sha256"]:
                problems.append(finding("snapshot_digest_mismatch", entity, snapshot["path"]))
                continue
            try:
                document = json.loads(data)
                count = len(document["current"]["records"])
            except (ValueError, KeyError, TypeError):
                problems.append(finding("snapshot_invalid", entity, snapshot["path"]))
                continue
            if count != snapshot["record_count"]:
                problems.append(finding("snapshot_count_mismatch", entity, str(count)))
        return problems

    def run(self) -> dict[str, Any]:
        sections = {"protocols": self.protocol_integrity(),
                    "instruments": self.instrument_integrity(),
                    "batches": self.batch_integrity(),
                    "observations": self.observation_integrity(),
                    "snapshots": self.snapshot_integrity()}
        problems = [item for section in sections.values() for item in section]
        codes = Counter(item["code"] for item in problems)
        return {"ok": not problems, "finding_count": len(problems),
                "findings": problems, "by_code": dict(sorted(codes.items())),
                "checked": {section: len(items) for section, items in sections.items()}}
