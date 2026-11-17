import unittest

from labtrace.contracts import (Channel, Instrument, LabError, Manifest, Page,
                                Protocol, digest_document, identifier, positive,
                                timestamp)
from tests.helpers import instrument, protocol


class ContractTests(unittest.TestCase):
    def test_protocol_roundtrip_and_digest(self):
        value = Protocol.parse(protocol())
        self.assertEqual(value.channel("signal").unit, "µM")
        self.assertEqual(value.document()["revision"], 1)
        self.assertEqual(digest_document(value.document()), digest_document(value.document()))

    def test_duplicate_channel_is_rejected(self):
        doc = protocol()
        doc["channels"].append(dict(doc["channels"][0]))
        with self.assertRaisesRegex(LabError, "duplicate_channel"):
            Protocol.parse(doc)

    def test_range_requires_strict_increase(self):
        for low, high in [("10", "10"), ("11", "10")]:
            with self.subTest(low=low, high=high):
                with self.assertRaisesRegex(LabError, "invalid_range"):
                    Channel.parse({"key": "signal", "unit": "µM",
                                   "minimum": low, "maximum": high})

    def test_nonfinite_channel_maximum_rejected(self):
        for value in ["Infinity", "NaN", "0", "-1"]:
            with self.subTest(value=value):
                with self.assertRaises(LabError):
                    Channel.parse({"key": "signal", "unit": "µM", "maximum": value})

    def test_field_mapping_must_be_unique(self):
        doc = instrument(field_map={"sample": "field", "well": "field"})
        with self.assertRaisesRegex(LabError, "duplicate_field_mapping"):
            Instrument.parse(doc)

    def test_instrument_rejects_unknown_format(self):
        with self.assertRaisesRegex(LabError, "invalid_formats"):
            Instrument.parse(instrument(formats=["xlsx"]))

    def test_instrument_rejects_invalid_trust(self):
        for trust in [-1, 101, "high", True]:
            with self.subTest(trust=trust):
                with self.assertRaises(LabError):
                    Instrument.parse(instrument(trust=trust))

    def test_page_path_rejects_escape(self):
        digest = "a" * 64
        for path in ["../outside.csv", "/tmp/absolute.csv", "folder/../../x.json", "C:\\x.csv"]:
            with self.subTest(path=path):
                with self.assertRaisesRegex(LabError, "unsafe_page_path"):
                    Page.parse({"ordinal": 1, "path": path, "format": "csv", "sha256": digest})

    def test_page_digest_requires_hex(self):
        with self.assertRaisesRegex(LabError, "invalid_digest"):
            Page.parse({"ordinal": 1, "path": "p.json", "format": "json", "sha256": "x" * 64})

    def test_manifest_requires_contiguous_pages(self):
        page = {"ordinal": 2, "path": "p.json", "format": "json", "sha256": "a" * 64}
        doc = {"instrument": "reader1", "batch": "B1", "protocol_key": "assay",
               "protocol_revision": 1, "plate": "P1", "pages": [page]}
        with self.assertRaisesRegex(LabError, "noncontiguous_pages"):
            Manifest.parse(doc)

    def test_timestamp_requires_timezone(self):
        with self.assertRaisesRegex(LabError, "invalid_time"):
            timestamp("2026-11-17T02:00:00", "measured_at")
        self.assertEqual(timestamp("2026-11-17T10:00:00+08:00", "measured_at"),
                         "2026-11-17T02:00:00Z")

    def test_identifier_rejects_whitespace(self):
        with self.assertRaisesRegex(LabError, "invalid_identifier"):
            identifier(" reader ", "instrument")

    def test_positive_excludes_boolean_and_zero(self):
        with self.assertRaises(LabError):
            positive("0", "maximum")
        self.assertEqual(positive("2.5", "maximum"), 2.5)


if __name__ == "__main__":
    unittest.main()
