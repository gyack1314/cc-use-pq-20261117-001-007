from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .contracts import digest_bytes
from .ingest import Harvester
from .release import Delivery, verify_snapshot
from .store import Store


def run_demo() -> dict:
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        page = base / "page.json"
        page.write_text(json.dumps({"rows": [
            {"sample": "S1", "well": "A1", "channel": "signal", "value": "250",
             "unit": "nM", "measured_at": "2026-11-17T02:00:00Z"},
            {"sample": "S2", "well": "A2", "channel": "signal", "value": "2",
             "unit": "mM", "measured_at": "2026-11-17T02:00:00Z"}]}), encoding="utf-8")
        manifest = {"instrument": "demo-reader", "batch": "B1", "protocol_key": "assay",
                    "protocol_revision": 1, "plate": "P1",
                    "pages": [{"ordinal": 1, "path": "page.json", "format": "json",
                               "sha256": digest_bytes(page.read_bytes())}]}
        manifest_path = base / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with Store(base / "lab.sqlite3") as store:
            harvest = Harvester(store)
            harvest.register_protocol({"key": "assay", "revision": 1,
                                       "channels": [{"key": "signal", "unit": "µM",
                                                     "minimum": "0", "maximum": "1000",
                                                     "tolerance": "0.01"}]})
            harvest.register_instrument({"key": "demo-reader", "protocol_key": "assay",
                                         "protocol_revision": 1, "formats": ["json"]})
            accepted = harvest.ingest(manifest_path)
            assert accepted["state"] == "complete", accepted
            delivery = Delivery(store)
            before = delivery.current()
            assert before["usable"] == 1 and before["quality"]["quarantined"] == 1, before
            snapshot = delivery.write_snapshot(base / "snapshot.json")
            checked = verify_snapshot(base / "snapshot.json", snapshot["sha256"])
            harvest.withdraw("demo-reader", "synthetic demonstration")
            after = delivery.current()
            assert after["usable"] == 0, after
            assert checked["summary"]["usable"] == 1, checked
            return {"ingest": accepted["state"], "before": before["usable"],
                    "after_withdrawal": after["usable"], "snapshot": checked["sha256"]}


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, sort_keys=True))
