from __future__ import annotations

from typing import Any

from .audit import Auditor
from .report import Reporter
from .store import Store


class Readiness:
    def __init__(self, store: Store):
        self.store = store

    def evaluate(self) -> dict[str, Any]:
        integrity = Auditor(self.store).run()
        exceptions = Reporter(self.store).exception_report()
        coverage = Reporter(self.store).coverage_report()
        blockers = []
        if not integrity["ok"]:
            blockers.append({"code": "integrity", "count": integrity["finding_count"]})
        if exceptions["failed_pages"]:
            blockers.append({"code": "failed_pages", "count": len(exceptions["failed_pages"])})
        if exceptions["conflicting_entities"]:
            blockers.append({"code": "conflicts", "count": len(exceptions["conflicting_entities"])})
        if coverage["unresolved_batches"]:
            blockers.append({"code": "unresolved_batches", "count": coverage["unresolved_batches"]})
        if not coverage["total_observations"]:
            blockers.append({"code": "no_observations", "count": 1})
        return {"ready": not blockers, "blockers": blockers,
                "coverage": coverage, "integrity": integrity,
                "exception_count": len(exceptions["failed_pages"]) + len(exceptions["conflicting_entities"])}

    def explain(self) -> list[str]:
        result = self.evaluate()
        if result["ready"]:
            return ["All recorded pages are complete, integrity holds, and no live conflict remains."]
        messages = []
        for blocker in result["blockers"]:
            code = blocker["code"]
            if code == "integrity":
                messages.append(f"Stored evidence has {blocker['count']} integrity finding(s).")
            elif code == "failed_pages":
                messages.append(f"{blocker['count']} page(s) still need repair or retry.")
            elif code == "conflicts":
                messages.append(f"{blocker['count']} entity conflict(s) have no publishable winner.")
            elif code == "unresolved_batches":
                messages.append(f"{blocker['count']} batch(es) remain incomplete.")
            elif code == "no_observations":
                messages.append("No measurements have been ingested.")
        return messages
