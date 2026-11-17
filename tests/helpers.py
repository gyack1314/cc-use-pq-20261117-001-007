from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

from labtrace.contracts import digest_bytes
from labtrace.ingest import Harvester
from labtrace.release import Delivery
from labtrace.store import Store


TIME = "2026-11-17T02:00:00Z"


def row(sample="S1", well="A1", value="0.25", unit="µM", channel="signal",
        measured_at=TIME, **extra):
    return {"sample": sample, "well": well, "value": value, "unit": unit,
            "channel": channel, "measured_at": measured_at, **extra}


def protocol(key="assay", revision=1, tolerance="0.01", maximum="10", **extra):
    return {"key": key, "revision": revision,
            "channels": [{"key": "signal", "unit": "µM", "minimum": "0",
                          "maximum": maximum, "tolerance": tolerance}], **extra}


def instrument(key="reader1", protocol_key="assay", protocol_revision=1,
               formats=None, trust=10, **extra):
    return {"key": key, "protocol_key": protocol_key,
            "protocol_revision": protocol_revision,
            "formats": formats or ["json", "csv", "html"], "trust": trust, **extra}


class LabCase:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = Store(self.base / "evidence.sqlite3")
        self.harvester = Harvester(self.store)
        self.delivery = Delivery(self.store)
        self.harvester.register_protocol(protocol())
        self.harvester.register_instrument(instrument())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def file(self, name, rows, format_name="json"):
        path = self.base / name
        if format_name == "json":
            path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
        elif format_name == "csv":
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        else:
            header = "".join(f"<th>{key}</th>" for key in rows[0])
            body = "".join("<tr>" + "".join(f"<td>{value}</td>" for value in item.values()) + "</tr>"
                           for item in rows)
            path.write_text(f"<table><tr>{header}</tr>{body}</table>", encoding="utf-8")
        return {"ordinal": 1, "path": name, "format": format_name,
                "sha256": digest_bytes(path.read_bytes())}

    def manifest(self, batch="B1", pages=None, instrument_key="reader1", plate="P1"):
        doc = {"instrument": instrument_key, "batch": batch,
               "protocol_key": "assay", "protocol_revision": 1,
               "plate": plate, "pages": pages or [self.file("page.json", [row()])]}
        path = self.base / f"manifest-{instrument_key}-{batch}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def ingest_rows(self, rows, *, batch="B1", instrument_key="reader1", format_name="json",
                    plate="P1"):
        name = f"{instrument_key}-{batch}.{format_name}"
        page = self.file(name, rows, format_name)
        path = self.manifest(batch, [page], instrument_key, plate)
        return self.harvester.ingest(path)
