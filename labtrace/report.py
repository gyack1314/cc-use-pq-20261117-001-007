from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from .audit import Auditor
from .release import Delivery
from .resolve import explain_result
from .store import Store


def _entity(row: dict[str, Any]) -> str:
    return row["plate"] + "/" + row["sample"] + "/" + row["well"] + "/" + row["channel"]


class Reporter:
    def __init__(self, store: Store):
        self.store = store
        self.delivery = Delivery(store)

    def batch_report(self, instrument: str, batch: str) -> dict[str, Any]:
        manifest = self.store.batch(instrument, batch)
        rows = [row for row in self.store.observations(instrument=instrument)
                if row["batch"] == batch]
        by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_page[row["ordinal"]].append(row)
        pages = []
        for page in manifest["pages"]:
            observations = by_page[page["ordinal"]]
            status = Counter(item["status"] for item in observations)
            pages.append({"ordinal": page["ordinal"], "state": page["state"],
                          "error_code": page["error_code"], "attempts": page["attempts"],
                          "declared_sha256": page["expected_sha256"],
                          "row_count": page["row_count"], "observed_rows": len(observations),
                          "quality": dict(sorted(status.items()))})
        affected = {row["identity"] for row in rows}
        current = self.delivery.current(include_excluded=True)
        resolutions = [item for item in current["records"] if item["identity"] in affected]
        return {"instrument": instrument, "batch": batch, "state": manifest["state"],
                "manifest_digest": manifest["digest"], "plate": manifest["plate"],
                "pages": pages, "observation_count": len(rows),
                "affected_resolutions": [{"identity": item["identity"],
                                          "state": item["state"],
                                          "explanation": explain_result(item)} for item in resolutions]}

    def instrument_report(self, instrument: str) -> dict[str, Any]:
        state = self.store.instrument_state(instrument)
        batches = self.store.batches(instrument)
        rows = self.store.observations(instrument=instrument)
        quality = Counter(row["status"] for row in rows)
        failures = []
        for batch in batches:
            for page in batch["pages"]:
                if page["state"] == "failed":
                    failures.append({"batch": batch["batch"], "ordinal": page["ordinal"],
                                     "code": page["error_code"], "detail": page["error_detail"]})
        return {"instrument": instrument, "trust": state["trust"],
                "withdrawn_at": state["withdrawn_at"],
                "withdrawal_reason": state["withdrawal_reason"],
                "batch_count": len(batches), "observation_count": len(rows),
                "quality": dict(sorted(quality.items())), "failed_pages": failures,
                "manifest_digests": {item["batch"]: item["digest"] for item in batches}}

    def plate_report(self, plate: str) -> dict[str, Any]:
        rows = [row for row in self.store.observations() if row["plate"] == plate]
        current = self.delivery.current(include_excluded=True)
        results = [item for item in current["records"] if item["identity"].startswith(plate + "|")]
        by_channel: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for item in results:
            channel = item["identity"].split("|")[3]
            by_channel[channel][item["state"]] += 1
        return {"plate": plate, "observation_count": len(rows),
                "sample_count": len({row["sample"] for row in rows}),
                "well_count": len({row["well"] for row in rows}),
                "channels": {key: dict(sorted(value.items())) for key, value in sorted(by_channel.items())},
                "resolutions": [{"identity": item["identity"], "state": item["state"],
                                 "winner": item["winner"], "explanation": explain_result(item)}
                                for item in results]}

    def exception_report(self) -> dict[str, Any]:
        batches = self.store.batches()
        failed_pages = []
        for batch in batches:
            for page in batch["pages"]:
                if page["state"] == "failed":
                    failed_pages.append({"instrument": batch["instrument"],
                                         "batch": batch["batch"], "ordinal": page["ordinal"],
                                         "error_code": page["error_code"],
                                         "attempts": page["attempts"]})
        current = self.delivery.current(include_excluded=True)
        conflict = [item for item in current["records"] if item["state"] == "conflict"]
        excluded = [item for item in current["records"] if item["state"] == "excluded"]
        audit = Auditor(self.store).run()
        return {"failed_pages": failed_pages,
                "conflicting_entities": [{"identity": item["identity"],
                                          "candidate_count": len(item["candidates"])} for item in conflict],
                "excluded_entities": [{"identity": item["identity"],
                                       "reasons": sorted({entry["reason"] for entry in item["excluded"]})}
                                      for item in excluded],
                "integrity_findings": audit["findings"],
                "ready_for_snapshot": not failed_pages and not conflict and audit["ok"]}

    def coverage_report(self) -> dict[str, Any]:
        batches = self.store.batches()
        observations = self.store.observations()
        by_instrument: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for batch in batches:
            counts = by_instrument[batch["instrument"]]
            counts["batches"] += 1
            for page in batch["pages"]:
                counts["pages"] += 1
                counts["pages_" + page["state"]] += 1
        for row in observations:
            by_instrument[row["instrument"]]["observations"] += 1
            by_instrument[row["instrument"]]["quality_" + row["status"]] += 1
        return {"instruments": {key: dict(sorted(value.items())) for key, value in sorted(by_instrument.items())},
                "total_batches": len(batches), "total_observations": len(observations),
                "unresolved_batches": sum(batch["state"] != "complete" for batch in batches)}
