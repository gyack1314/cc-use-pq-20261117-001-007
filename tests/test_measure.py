import unittest
from decimal import Decimal

from labtrace.adapters import extract, map_fields
from labtrace.contracts import Channel, LabError
from labtrace.measure import (comparable, measurement, normalized_value,
                              parse_identity, value_bucket)
from labtrace.quality import assess, batch_state, exposure, quality_summary
from tests.helpers import row


CHANNEL = Channel.parse({"key": "signal", "unit": "µM", "minimum": "0",
                         "maximum": "10", "tolerance": "0.01"})


class AdapterTests(unittest.TestCase):
    def test_json_list_and_wrapper(self):
        for value in ['[{"sample":"S1"}]', '{"rows":[{"sample":"S1"}]}']:
            with self.subTest(value=value):
                self.assertEqual(extract(value.encode(), "json", limit=10)[0]["sample"], "S1")

    def test_csv_header_and_content(self):
        data = b"sample,well\nS1,A1\nS2,A2\n"
        result = extract(data, "csv", limit=10)
        self.assertEqual([item["well"] for item in result], ["A1", "A2"])

    def test_html_table(self):
        data = b"<table><tr><th>sample</th><th>well</th></tr><tr><td>S1</td><td>A1</td></tr></table>"
        self.assertEqual(extract(data, "html", limit=10), [{"sample": "S1", "well": "A1"}])

    def test_html_rejects_column_mismatch(self):
        data = b"<table><tr><th>sample</th><th>well</th></tr><tr><td>S1</td></tr></table>"
        with self.assertRaisesRegex(LabError, "invalid_column_count"):
            extract(data, "html", limit=10)

    def test_html_rejects_two_tables(self):
        data = b"<table><tr><th>a</th></tr></table><table><tr><th>b</th></tr></table>"
        with self.assertRaisesRegex(LabError, "invalid_table"):
            extract(data, "html", limit=10)

    def test_csv_rejects_duplicate_header(self):
        with self.assertRaisesRegex(LabError, "duplicate_header"):
            extract(b"sample,sample\nS1,S2\n", "csv", limit=10)

    def test_page_limit(self):
        with self.assertRaisesRegex(LabError, "page_row_limit"):
            extract(b'[{"a":1},{"a":2}]', "json", limit=1)

    def test_invalid_encoding(self):
        with self.assertRaisesRegex(LabError, "invalid_encoding"):
            extract(b"\xff", "json", limit=10)

    def test_missing_mapped_field(self):
        with self.assertRaisesRegex(LabError, "missing_field"):
            map_fields({"sample_id": "S1"}, {"sample": "sample_id"}, ("sample", "well"))

    def test_field_mapping(self):
        self.assertEqual(map_fields({"sample_id": "S1", "well": "A1"},
                                    {"sample": "sample_id"}, ("sample", "well")),
                         {"sample": "S1", "well": "A1"})


class MeasurementTests(unittest.TestCase):
    def test_nan_and_infinity_rejected(self):
        for value in ["NaN", "Infinity", "-Infinity", None, True]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(LabError, "invalid_number"):
                    normalized_value(value, "µM", CHANNEL)

    def test_unit_conversion(self):
        self.assertEqual(normalized_value("250", "nM", CHANNEL), Decimal("0.250"))
        self.assertEqual(normalized_value("0.25", "µM", CHANNEL), Decimal("0.25"))

    def test_incompatible_unit(self):
        with self.assertRaisesRegex(LabError, "incompatible_unit"):
            normalized_value("1", "%", CHANNEL)

    def test_unknown_unit(self):
        with self.assertRaisesRegex(LabError, "unknown_unit"):
            normalized_value("1", "lightyears", CHANNEL)

    def test_identity_contains_plate_and_replicate(self):
        doc = measurement({**row(), "replicate": "r2"}, CHANNEL, "P1")
        self.assertEqual(parse_identity(doc["identity"]), ("P1", "S1", "A1", "signal", "r2"))

    def test_well_boundary(self):
        for well in ["A0", "Q1", "A25", "a1", "A01"]:
            with self.subTest(well=well):
                with self.assertRaisesRegex(LabError, "invalid_well"):
                    measurement(row(well=well), CHANNEL, "P1")

    def test_control_kind(self):
        with self.assertRaisesRegex(LabError, "invalid_control"):
            measurement({**row(), "control": "mystery"}, CHANNEL, "P1")

    def test_measurement_normalizes_timezone(self):
        doc = measurement(row(measured_at="2026-11-17T10:00:00+08:00"), CHANNEL, "P1")
        self.assertEqual(doc["measured_at"], "2026-11-17T02:00:00Z")

    def test_comparability_uses_tolerance(self):
        left = measurement(row(value="0.25"), CHANNEL, "P1")
        right = measurement(row(value="0.255"), CHANNEL, "P1")
        self.assertTrue(comparable(left, right, CHANNEL))
        third = measurement(row(value="0.4"), CHANNEL, "P1")
        self.assertFalse(comparable(left, third, CHANNEL))

    def test_quality_out_of_range(self):
        doc = measurement(row(value="20"), CHANNEL, "P1")
        self.assertEqual(assess(doc, CHANNEL).reason, "out_of_range")

    def test_quality_control_and_unknown_sample(self):
        control = measurement({**row(), "control": "blank"}, CHANNEL, "P1")
        self.assertEqual(assess(control, CHANNEL).status, "control")
        unknown = measurement(row(sample="unknownS1"), CHANNEL, "P1")
        self.assertEqual(assess(unknown, CHANNEL).status, "quarantined")

    def test_batch_state(self):
        self.assertEqual(batch_state([{"state": "committed"}, {"state": "failed"}]), "partial_failed")
        self.assertEqual(batch_state([{"state": "committed"}]), "complete")
        self.assertEqual(batch_state([]), "pending")

    def test_exposure_and_summary(self):
        self.assertTrue(exposure("eligible", False))
        self.assertFalse(exposure("eligible", True))
        self.assertFalse(exposure("control", False))
        self.assertEqual(quality_summary([{"status": "eligible"}, {"status": "control"}]),
                         {"eligible": 1, "quarantined": 0, "control": 1})

    def test_value_bucket(self):
        self.assertEqual(value_bucket("1.2344"), "1.234")
        with self.assertRaisesRegex(LabError, "invalid_precision"):
            value_bucket("1", "0")


if __name__ == "__main__":
    unittest.main()
