import json
import unittest
from pathlib import Path

from labtrace.contracts import LabError, digest_bytes
from labtrace.ingest import Harvester
from labtrace.store import Store
from tests.helpers import LabCase, instrument, protocol, row


class IngestTests(LabCase, unittest.TestCase):
    def test_complete_json_batch(self):
        result = self.ingest_rows([row()])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["pages"][0]["rows"], 1)
        self.assertEqual(self.delivery.current()["usable"], 1)

    def test_csv_batch(self):
        result = self.ingest_rows([row(value="250", unit="nM")], format_name="csv")
        self.assertEqual(result["state"], "complete")
        self.assertEqual(self.store.observations()[0]["value"], "0.250")

    def test_html_batch(self):
        result = self.ingest_rows([row(value="0.25")], format_name="html")
        self.assertEqual(result["state"], "complete")
        self.assertEqual(self.store.observations()[0]["sample"], "S1")

    def test_missing_page_recovers_without_duplicate(self):
        page1 = self.file("first.json", [row()])
        page2 = dict(page1, ordinal=2, path="second.json")
        path = self.manifest(pages=[page1, page2])
        first = self.harvester.ingest(path)
        self.assertEqual(first["state"], "partial_failed")
        self.assertEqual(first["pages"][1]["error"]["code"], "page_missing")
        self.assertEqual(self.delivery.summary()["observation_count"], 1)
        (self.base / "second.json").write_bytes((self.base / "first.json").read_bytes())
        retry = self.harvester.resume(path)
        self.assertEqual(retry["state"], "complete")
        self.assertEqual(self.delivery.summary()["observation_count"], 2)
        self.assertEqual(self.store.page("reader1", "B1", 1)["attempts"], 1)

    def test_digest_mismatch_then_restore(self):
        page = self.file("page.json", [row()])
        path = self.manifest(pages=[page])
        original = (self.base / "page.json").read_bytes()
        (self.base / "page.json").write_text("tampered", encoding="utf-8")
        failed = self.harvester.ingest(path)
        self.assertEqual(failed["pages"][0]["error"]["code"], "page_digest_mismatch")
        self.assertEqual(self.store.observations(), [])
        (self.base / "page.json").write_bytes(original)
        self.assertEqual(self.harvester.retry_page(path, 1)["state"], "complete")
        self.assertEqual(self.store.page("reader1", "B1", 1)["attempts"], 2)

    def test_invalid_row_fails_entire_page(self):
        result = self.ingest_rows([row(), row(sample="S2", well="A0")])
        self.assertEqual(result["pages"][0]["error"]["code"], "row_invalid")
        self.assertEqual(self.store.observations(), [])
        self.assertIn("row 2", result["pages"][0]["error"]["detail"])

    def test_duplicate_measurement_fails_page(self):
        result = self.ingest_rows([row(), row()])
        self.assertEqual(result["pages"][0]["error"]["code"], "duplicate_measurement")
        self.assertEqual(self.store.observations(), [])

    def test_quality_quarantines_without_failing_page(self):
        result = self.ingest_rows([row(value="30"), row(sample="S2", well="A2")])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(self.delivery.current()["usable"], 1)
        self.assertEqual(self.store.observations()[0]["status"], "quarantined")

    def test_idempotent_reingest(self):
        path = self.manifest()
        self.harvester.ingest(path)
        first = self.store.page("reader1", "B1", 1)
        self.harvester.ingest(path)
        second = self.store.page("reader1", "B1", 1)
        self.assertEqual(first["attempts"], second["attempts"])
        self.assertEqual(len(self.store.observations()), 1)

    def test_manifest_mutation_rejected(self):
        path = self.manifest()
        self.harvester.ingest(path)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["plate"] = "P2"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(LabError, "batch_manifest_conflict"):
            self.harvester.ingest(path)
        self.assertEqual(self.store.observations()[0]["plate"], "P1")

    def test_protocol_revision_conflict(self):
        with self.assertRaisesRegex(LabError, "protocol_revision_conflict"):
            self.harvester.register_protocol(protocol(maximum="100"))

    def test_protocol_revision_idempotent(self):
        result = self.harvester.register_protocol(protocol())
        self.assertFalse(result["created"])
        self.assertEqual(len(self.store.protocols()), 1)

    def test_instrument_declaration_conflict(self):
        with self.assertRaisesRegex(LabError, "instrument_conflict"):
            self.harvester.register_instrument(instrument(trust=99))

    def test_instrument_declaration_idempotent(self):
        self.assertFalse(self.harvester.register_instrument(instrument())["created"])

    def test_unknown_instrument_rejected(self):
        path = self.manifest(instrument_key="unknown")
        with self.assertRaisesRegex(LabError, "unknown_instrument"):
            self.harvester.ingest(path)

    def test_format_not_allowed_for_instrument(self):
        self.harvester.register_instrument(instrument(key="json-only", formats=["json"]))
        page = self.file("x.csv", [row()], "csv")
        path = self.manifest(instrument_key="json-only", pages=[page])
        with self.assertRaisesRegex(LabError, "unsupported_instrument_format"):
            self.harvester.ingest(path)

    def test_stop_on_error_leaves_later_page_pending(self):
        page1 = {"ordinal": 1, "path": "missing.json", "format": "json", "sha256": "a" * 64}
        page2 = dict(self.file("second.json", [row()]), ordinal=2)
        path = self.manifest(pages=[page1, page2])
        first = self.harvester.ingest(path, stop_on_error=True)
        self.assertEqual(len(first["pages"]), 1)
        self.assertEqual(self.store.page("reader1", "B1", 2)["state"], "pending")
        self.harvester.resume(path)
        self.assertEqual(self.store.page("reader1", "B1", 2)["state"], "committed")

    def test_retry_page_only_targets_selected_page(self):
        page1 = self.file("first.json", [row()])
        page2 = dict(page1, ordinal=2, path="missing.json")
        path = self.manifest(pages=[page1, page2])
        self.harvester.ingest(path)
        self.harvester.retry_page(path, 2)
        self.assertEqual(self.store.page("reader1", "B1", 1)["attempts"], 1)
        self.assertEqual(self.store.page("reader1", "B1", 2)["attempts"], 2)

    def test_retry_unknown_page_rejected(self):
        path = self.manifest()
        with self.assertRaisesRegex(LabError, "unknown_page"):
            self.harvester.retry_page(path, 3)

    def test_withdrawn_instrument_cannot_ingest_new_batch(self):
        self.harvester.withdraw("reader1", "retired")
        with self.assertRaisesRegex(LabError, "instrument_withdrawn"):
            self.harvester.ingest(self.manifest())

    def test_page_quality_matches_observations(self):
        path = self.manifest()
        self.harvester.ingest(path)
        quality = self.harvester.page_quality("reader1", "B1", 1)
        self.assertTrue(quality["row_count_matches"])
        self.assertEqual(quality["counts"]["eligible"], 1)

    def test_manifest_missing(self):
        with self.assertRaisesRegex(LabError, "manifest_missing"):
            self.harvester.ingest(self.base / "missing-manifest.json")

    def test_invalid_manifest_json(self):
        path = self.base / "bad-manifest.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(LabError, "invalid_manifest"):
            self.harvester.ingest(path)

    def test_observation_raw_evidence_kept(self):
        self.ingest_rows([row(note="synthetic payload")])
        self.assertEqual(self.store.observations()[0]["raw"]["note"], "synthetic payload")


if __name__ == "__main__":
    unittest.main()
