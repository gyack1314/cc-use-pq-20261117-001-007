import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from labtrace.api import create_app
from labtrace.contracts import digest_bytes
from tests.helpers import instrument, protocol, row


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.client = TestClient(create_app(self.base / "api.sqlite3"))
        assert self.client.post("/protocols", json=protocol()).status_code == 200
        assert self.client.post("/instruments", json=instrument()).status_code == 200

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def manifest(self, rows=None, batch="B1", instrument_key="reader1", filename="page.json"):
        content = json.dumps({"rows": rows or [row()]}, ensure_ascii=False)
        page = self.base / filename
        page.write_text(content, encoding="utf-8")
        doc = {"instrument": instrument_key, "batch": batch,
               "protocol_key": "assay", "protocol_revision": 1,
               "plate": "P1", "pages": [{"ordinal": 1, "path": filename,
                                       "format": "json", "sha256": digest_bytes(page.read_bytes())}]}
        manifest = self.base / f"manifest-{batch}.json"
        manifest.write_text(json.dumps(doc), encoding="utf-8")
        return manifest

    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_registration_is_idempotent(self):
        self.assertFalse(self.client.post("/protocols", json=protocol()).json()["created"])
        self.assertFalse(self.client.post("/instruments", json=instrument()).json()["created"])

    def test_conflicting_registration_is_409(self):
        response = self.client.post("/instruments", json=instrument(trust=20))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "instrument_conflict")

    def test_ingest_and_current(self):
        manifest = self.manifest()
        response = self.client.post("/batches/ingest", json={"path": str(manifest)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "complete")
        self.assertEqual(self.client.get("/current").json()["usable"], 1)

    def test_batch_and_quality_routes(self):
        manifest = self.manifest()
        self.client.post("/batches/ingest", json={"path": str(manifest)})
        batch = self.client.get("/batches/reader1/B1").json()
        self.assertEqual(batch["pages"][0]["state"], "committed")
        quality = self.client.get("/batches/reader1/B1/pages/1/quality").json()
        self.assertTrue(quality["row_count_matches"])

    def test_missing_manifest_is_422(self):
        response = self.client.post("/batches/ingest", json={"path": str(self.base / "missing.json")})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "manifest_missing")

    def test_unknown_batch_is_422(self):
        response = self.client.get("/batches/reader1/missing")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "unknown_batch")

    def test_history_and_trace(self):
        manifest = self.manifest()
        self.client.post("/batches/ingest", json={"path": str(manifest)})
        history = self.client.get("/history", params={"instrument": "reader1"}).json()
        self.assertEqual(len(history["observations"]), 1)
        trace = self.client.get("/trace", params={"identity": "P1|S1|A1|signal|primary"}).json()
        self.assertEqual(trace["resolution"]["state"], "single_source")

    def test_snapshot_route_and_digest(self):
        manifest = self.manifest()
        self.client.post("/batches/ingest", json={"path": str(manifest)})
        path = self.base / "snapshot.json"
        record = self.client.post("/snapshots", json={"path": str(path)}).json()
        verify = self.client.get("/snapshots/verify", params={"path": str(path),
                                                            "sha256": record["sha256"]})
        self.assertEqual(verify.status_code, 200)
        self.assertEqual(verify.json()["summary"]["usable"], 1)

    def test_withdrawal_changes_current_but_not_history(self):
        manifest = self.manifest()
        self.client.post("/batches/ingest", json={"path": str(manifest)})
        response = self.client.post("/instruments/reader1/withdraw", json={"reason": "retired"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/current").json()["usable"], 0)
        self.assertEqual(len(self.client.get("/history").json()["observations"]), 1)

    def test_retry_after_missing_page(self):
        manifest = self.manifest()
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        (self.base / "page.json").unlink()
        manifest.write_text(json.dumps(doc), encoding="utf-8")
        first = self.client.post("/batches/ingest", json={"path": str(manifest)}).json()
        self.assertEqual(first["state"], "partial_failed")
        (self.base / "page.json").write_text(json.dumps({"rows": [row()]}, ensure_ascii=False), encoding="utf-8")
        second = self.client.post("/batches/retry/1", json={"path": str(manifest)}).json()
        self.assertEqual(second["state"], "complete")

    def test_summary(self):
        self.client.post("/batches/ingest", json={"path": str(self.manifest())})
        summary = self.client.get("/summary").json()
        self.assertEqual(summary["usable"], 1)
        self.assertEqual(summary["observation_count"], 1)


if __name__ == "__main__":
    unittest.main()
