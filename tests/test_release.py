import json
import unittest
from pathlib import Path

from labtrace.contracts import LabError
from labtrace.release import verify_snapshot
from tests.helpers import LabCase, instrument, row


class ReleaseTests(LabCase, unittest.TestCase):
    def test_one_instrument_is_usable(self):
        self.ingest_rows([row()])
        current = self.delivery.current()
        self.assertEqual(current["usable"], 1)
        self.assertEqual(current["records"][0]["state"], "single_source")

    def test_matching_instruments_confirm(self):
        self.ingest_rows([row(value="0.25")])
        self.harvester.register_instrument(instrument(key="reader2", trust=20))
        self.ingest_rows([row(value="0.255")], instrument_key="reader2")
        current = self.delivery.current()
        self.assertEqual(current["records"][0]["state"], "confirmed")
        self.assertEqual(current["records"][0]["winner"]["instrument"], "reader2")

    def test_conflicting_instruments_withhold_value(self):
        self.ingest_rows([row(value="0.25")])
        self.harvester.register_instrument(instrument(key="reader2"))
        self.ingest_rows([row(value="0.50")], instrument_key="reader2")
        current = self.delivery.current()
        self.assertEqual(current["conflicts"], 1)
        self.assertEqual(current["usable"], 0)
        self.assertEqual(len(current["records"][0]["candidates"]), 2)

    def test_withdrawal_resolves_conflict(self):
        self.ingest_rows([row(value="0.25")])
        self.harvester.register_instrument(instrument(key="reader2"))
        self.ingest_rows([row(value="0.50")], instrument_key="reader2")
        self.assertEqual(self.delivery.current()["conflicts"], 1)
        self.harvester.withdraw("reader2", "synthetic calibration issue")
        after = self.delivery.current()
        self.assertEqual(after["conflicts"], 0)
        self.assertEqual(after["usable"], 1)
        self.assertEqual(after["records"][0]["winner"]["instrument"], "reader1")

    def test_all_withdrawn_are_excluded(self):
        self.ingest_rows([row()])
        self.harvester.withdraw("reader1", "retired")
        self.assertEqual(self.delivery.current()["records"], [])
        detail = self.delivery.current(include_excluded=True)["records"][0]
        self.assertEqual(detail["state"], "excluded")
        self.assertEqual(detail["excluded"][0]["reason"], "source_withdrawn")

    def test_quality_quarantine_not_usable(self):
        self.ingest_rows([row(value="99")])
        self.assertEqual(self.delivery.current()["usable"], 0)
        detail = self.delivery.current(include_excluded=True)["records"][0]
        self.assertEqual(detail["excluded"][0]["reason"], "out_of_range")

    def test_control_not_usable(self):
        self.ingest_rows([row(control="blank")])
        detail = self.delivery.current(include_excluded=True)["records"][0]
        self.assertEqual(detail["excluded"][0]["reason"], "control_measurement")

    def test_history_keeps_raw_after_withdrawal(self):
        self.ingest_rows([row(note="evidence")])
        self.harvester.withdraw("reader1", "retired")
        history = self.delivery.history(instrument="reader1")
        self.assertEqual(history["observations"][0]["raw"]["note"], "evidence")
        self.assertEqual(history["observations"][0]["withdrawal_reason"], "retired")
        self.assertTrue(any(event["kind"] == "instrument_withdrawn" for event in history["events"]))

    def test_trace_reports_resolution(self):
        self.ingest_rows([row()])
        detail = self.delivery.trace("P1|S1|A1|signal|primary")
        self.assertEqual(detail["resolution"]["state"], "single_source")
        self.assertEqual(detail["observations"][0]["sample"], "S1")

    def test_empty_trace(self):
        detail = self.delivery.trace("P1|absent|A1|signal|primary")
        self.assertIsNone(detail["resolution"])
        self.assertEqual(detail["observations"], [])

    def test_latest_source_revision(self):
        self.ingest_rows([row(value="0.25")], batch="B1")
        self.ingest_rows([row(value="0.3", measured_at="2026-11-18T02:00:00Z")], batch="B2")
        current = self.delivery.current()["records"][0]
        self.assertEqual(current["winner"]["batch"], "B2")
        self.assertEqual(len(self.delivery.history()["observations"]), 2)

    def test_separate_plates_do_not_conflict(self):
        self.ingest_rows([row(value="0.25")], batch="B1", plate="P1")
        self.ingest_rows([row(value="0.5")], batch="B2", plate="P2")
        self.assertEqual(self.delivery.current()["usable"], 2)
        self.assertEqual(self.delivery.current()["conflicts"], 0)

    def test_separate_replicates_do_not_conflict(self):
        self.ingest_rows([row(replicate="r1"), row(well="A2", replicate="r2")])
        self.assertEqual(self.delivery.current()["usable"], 2)

    def test_snapshot_remains_old_after_withdrawal(self):
        self.ingest_rows([row()])
        path = self.base / "snap.json"
        record = self.delivery.write_snapshot(path)
        self.harvester.withdraw("reader1", "retired")
        self.assertEqual(self.delivery.current()["usable"], 0)
        checked = verify_snapshot(path, record["sha256"])
        self.assertEqual(checked["summary"]["usable"], 1)

    def test_snapshot_tampering_detected(self):
        self.ingest_rows([row()])
        path = self.base / "snap.json"
        record = self.delivery.write_snapshot(path)
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(LabError, "snapshot_digest_mismatch"):
            verify_snapshot(path, record["sha256"])

    def test_snapshot_cannot_overwrite(self):
        path = self.base / "snap.json"
        self.delivery.write_snapshot(path)
        with self.assertRaisesRegex(LabError, "snapshot_exists"):
            self.delivery.write_snapshot(path)

    def test_summary_counts_batches_and_pages(self):
        self.ingest_rows([row()], batch="B1")
        self.ingest_rows([row(sample="S2", well="A2")], batch="B2")
        summary = self.delivery.summary()
        self.assertEqual(summary["batch_count"], 2)
        self.assertEqual(summary["page_count"], 2)
        self.assertEqual(summary["observation_count"], 2)

    def test_withdrawal_is_idempotent(self):
        first = self.harvester.withdraw("reader1", "retired")
        second = self.harvester.withdraw("reader1", "different reason")
        self.assertEqual(first["withdrawn_at"], second["withdrawn_at"])
        self.assertEqual(second["withdrawal_reason"], "retired")

    def test_events_are_ordered(self):
        self.ingest_rows([row()])
        self.harvester.withdraw("reader1", "retired")
        events = self.store.events()
        self.assertEqual(events[-2]["kind"], "page_committed")
        self.assertEqual(events[-1]["kind"], "instrument_withdrawn")


if __name__ == "__main__":
    unittest.main()
