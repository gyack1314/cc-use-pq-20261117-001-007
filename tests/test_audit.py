import unittest

from labtrace.audit import Auditor
from labtrace.checks import Readiness
from labtrace.compare import Comparator, compare_views
from labtrace.report import Reporter
from tests.helpers import LabCase, instrument, row


class AuditTests(LabCase, unittest.TestCase):
    def test_empty_store_has_no_integrity_findings(self):
        report = Auditor(self.store).run()
        self.assertTrue(report["ok"])
        self.assertEqual(report["finding_count"], 0)

    def test_ingested_batch_has_consistent_page_counts(self):
        self.ingest_rows([row(), row(sample="S2", well="A2")])
        report = Auditor(self.store).run()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["findings"], [])

    def test_protocol_digest_change_detected(self):
        self.store.connection.execute("UPDATE protocols SET digest='wrong' WHERE key='assay'")
        codes = {item["code"] for item in Auditor(self.store).run()["findings"]}
        self.assertIn("protocol_digest_mismatch", codes)

    def test_instrument_digest_change_detected(self):
        self.store.connection.execute("UPDATE instruments SET digest='wrong' WHERE key='reader1'")
        codes = {item["code"] for item in Auditor(self.store).run()["findings"]}
        self.assertIn("instrument_digest_mismatch", codes)

    def test_page_count_change_detected(self):
        self.ingest_rows([row()])
        self.store.connection.execute("UPDATE pages SET row_count=7 WHERE instrument='reader1'")
        codes = {item["code"] for item in Auditor(self.store).run()["findings"]}
        self.assertIn("page_row_count_mismatch", codes)

    def test_batch_state_change_detected(self):
        self.ingest_rows([row()])
        self.store.connection.execute("UPDATE batches SET state='pending' WHERE instrument='reader1'")
        codes = {item["code"] for item in Auditor(self.store).run()["findings"]}
        self.assertIn("batch_state_mismatch", codes)

    def test_snapshot_tampering_detected_by_audit(self):
        self.ingest_rows([row()])
        path = self.base / "snapshot.json"
        self.delivery.write_snapshot(path)
        path.write_text("{}", encoding="utf-8")
        codes = {item["code"] for item in Auditor(self.store).run()["findings"]}
        self.assertIn("snapshot_digest_mismatch", codes)

    def test_batch_report_follows_quality_and_cursor(self):
        self.ingest_rows([row(), row(sample="S2", well="A2", value="99")])
        report = Reporter(self.store).batch_report("reader1", "B1")
        self.assertEqual(report["pages"][0]["row_count"], 2)
        self.assertEqual(report["pages"][0]["quality"]["quarantined"], 1)
        self.assertEqual(report["state"], "complete")

    def test_instrument_report_withdrawal(self):
        self.ingest_rows([row()])
        self.harvester.withdraw("reader1", "retired")
        report = Reporter(self.store).instrument_report("reader1")
        self.assertEqual(report["withdrawal_reason"], "retired")
        self.assertEqual(report["observation_count"], 1)

    def test_plate_report_counts_wells(self):
        self.ingest_rows([row(), row(sample="S2", well="A2")])
        report = Reporter(self.store).plate_report("P1")
        self.assertEqual(report["sample_count"], 2)
        self.assertEqual(report["well_count"], 2)
        self.assertEqual(len(report["resolutions"]), 2)

    def test_exception_report_flags_conflict(self):
        self.ingest_rows([row(value="0.25")])
        self.harvester.register_instrument(instrument(key="reader2"))
        self.ingest_rows([row(value="0.8")], instrument_key="reader2")
        report = Reporter(self.store).exception_report()
        self.assertFalse(report["ready_for_snapshot"])
        self.assertEqual(len(report["conflicting_entities"]), 1)

    def test_coverage_report(self):
        self.ingest_rows([row()])
        report = Reporter(self.store).coverage_report()
        self.assertEqual(report["total_observations"], 1)
        self.assertEqual(report["unresolved_batches"], 0)

    def test_readiness_rejects_empty_store(self):
        report = Readiness(self.store).evaluate()
        self.assertFalse(report["ready"])
        self.assertIn("no_observations", [item["code"] for item in report["blockers"]])

    def test_readiness_accepts_complete_store(self):
        self.ingest_rows([row()])
        report = Readiness(self.store).evaluate()
        self.assertTrue(report["ready"], report)

    def test_snapshot_comparison_after_withdrawal(self):
        self.ingest_rows([row()])
        snapshot = self.delivery.write_snapshot(self.base / "snap.json")
        self.harvester.withdraw("reader1", "retired")
        delta = Comparator(self.store).snapshot_to_current(snapshot["id"])
        self.assertEqual(delta["before_usable"], 1)
        self.assertEqual(delta["after_usable"], 0)
        self.assertEqual(len(delta["removed"]), 1)

    def test_batch_current_comparison(self):
        self.ingest_rows([row()])
        result = Comparator(self.store).batch_to_current("reader1", "B1")
        self.assertEqual(result["affected"][0]["state"], "single_source")

    def test_source_contribution(self):
        self.ingest_rows([row()])
        contribution = Comparator(self.store).source_contribution("reader1")
        self.assertEqual(contribution["selected_count"], 1)
        self.assertEqual(contribution["challenged_count"], 0)

    def test_compare_views_rejects_duplicate_identity(self):
        from labtrace.contracts import LabError
        before = {"records": [{"identity": "X", "state": "single_source"},
                              {"identity": "X", "state": "single_source"}]}
        with self.assertRaisesRegex(LabError, "invalid_comparison_record"):
            compare_views(before, {"records": []})


if __name__ == "__main__":
    unittest.main()
