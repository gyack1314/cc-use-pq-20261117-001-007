from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .contracts import Instrument, LabError, Manifest, Observation, Protocol, canonical, digest_document, now
from .quality import batch_state


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS protocols (
 key TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
 digest TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(key, revision)
);
CREATE TABLE IF NOT EXISTS instruments (
 key TEXT PRIMARY KEY, document TEXT NOT NULL, digest TEXT NOT NULL,
 protocol_key TEXT NOT NULL, protocol_revision INTEGER NOT NULL,
 trust INTEGER NOT NULL, withdrawn_at TEXT, withdrawal_reason TEXT,
 created_at TEXT NOT NULL,
 FOREIGN KEY(protocol_key, protocol_revision) REFERENCES protocols(key, revision)
);
CREATE TABLE IF NOT EXISTS batches (
 instrument TEXT NOT NULL, batch TEXT NOT NULL, manifest TEXT NOT NULL,
 digest TEXT NOT NULL, state TEXT NOT NULL, plate TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(instrument, batch), FOREIGN KEY(instrument) REFERENCES instruments(key)
);
CREATE TABLE IF NOT EXISTS pages (
 instrument TEXT NOT NULL, batch TEXT NOT NULL, ordinal INTEGER NOT NULL,
 path TEXT NOT NULL, format TEXT NOT NULL, expected_sha256 TEXT NOT NULL,
 state TEXT NOT NULL, error_code TEXT, error_detail TEXT, row_count INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, completed_at TEXT,
 PRIMARY KEY(instrument, batch, ordinal),
 FOREIGN KEY(instrument, batch) REFERENCES batches(instrument, batch)
);
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 instrument TEXT NOT NULL, batch TEXT NOT NULL, ordinal INTEGER NOT NULL,
 row_number INTEGER NOT NULL, plate TEXT NOT NULL, sample TEXT NOT NULL,
 well TEXT NOT NULL, channel TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL,
 measured_at TEXT NOT NULL, identity_key TEXT NOT NULL, status TEXT NOT NULL,
 reason TEXT NOT NULL, raw_json TEXT NOT NULL, observed_at TEXT NOT NULL,
 FOREIGN KEY(instrument, batch, ordinal) REFERENCES pages(instrument, batch, ordinal),
 UNIQUE(instrument, batch, ordinal, row_number)
);
CREATE INDEX IF NOT EXISTS obs_identity ON observations(identity_key);
CREATE INDEX IF NOT EXISTS obs_source ON observations(instrument, batch);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, instrument TEXT,
 batch TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL, record_count INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=20)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=20000")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _event(self, connection: sqlite3.Connection, kind: str,
               instrument: str | None, batch: str | None, payload: dict[str, Any]) -> None:
        connection.execute("INSERT INTO events(kind,instrument,batch,payload,created_at) VALUES(?,?,?,?,?)",
                           (kind, instrument, batch, canonical(payload), now()))

    def register_protocol(self, protocol: Protocol) -> dict[str, Any]:
        doc = protocol.document()
        digest = digest_document(doc)
        with self.transaction() as db:
            row = db.execute("SELECT digest FROM protocols WHERE key=? AND revision=?",
                             (protocol.key, protocol.revision)).fetchone()
            if row and row["digest"] != digest:
                raise LabError("protocol_revision_conflict", protocol.key)
            if not row:
                db.execute("INSERT INTO protocols VALUES(?,?,?,?,?)",
                           (protocol.key, protocol.revision, canonical(doc), digest, now()))
                self._event(db, "protocol_registered", None, None, {"key": protocol.key, "revision": protocol.revision})
        return {"protocol": doc, "digest": digest, "created": not bool(row)}

    def protocol(self, key: str, revision: int) -> Protocol:
        row = self.connection.execute("SELECT document FROM protocols WHERE key=? AND revision=?",
                                      (key, revision)).fetchone()
        if not row:
            raise LabError("unknown_protocol", f"{key}:{revision}")
        return Protocol.parse(json.loads(row["document"]))

    def protocols(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT document, digest FROM protocols ORDER BY key,revision").fetchall()
        return [{**json.loads(row["document"]), "digest": row["digest"]} for row in rows]

    def register_instrument(self, instrument: Instrument) -> dict[str, Any]:
        self.protocol(instrument.protocol_key, instrument.protocol_revision)
        doc = instrument.document()
        digest = digest_document(doc)
        with self.transaction() as db:
            row = db.execute("SELECT digest FROM instruments WHERE key=?", (instrument.key,)).fetchone()
            if row and row["digest"] != digest:
                raise LabError("instrument_conflict", instrument.key)
            if not row:
                db.execute("INSERT INTO instruments(key,document,digest,protocol_key,protocol_revision,trust,created_at) VALUES(?,?,?,?,?,?,?)",
                           (instrument.key, canonical(doc), digest, instrument.protocol_key,
                            instrument.protocol_revision, instrument.trust, now()))
                self._event(db, "instrument_registered", instrument.key, None, {"digest": digest})
        return {"instrument": doc, "digest": digest, "created": not bool(row)}

    def instrument(self, key: str) -> Instrument:
        row = self.connection.execute("SELECT document FROM instruments WHERE key=?", (key,)).fetchone()
        if not row:
            raise LabError("unknown_instrument", key)
        return Instrument.parse(json.loads(row["document"]))

    def instrument_state(self, key: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM instruments WHERE key=?", (key,)).fetchone()
        if not row:
            raise LabError("unknown_instrument", key)
        return {"key": key, "trust": row["trust"], "withdrawn_at": row["withdrawn_at"],
                "withdrawal_reason": row["withdrawal_reason"], "digest": row["digest"]}

    def ensure_batch(self, manifest: Manifest) -> dict[str, Any]:
        instrument = self.instrument(manifest.instrument)
        if (instrument.protocol_key, instrument.protocol_revision) != (manifest.protocol_key, manifest.protocol_revision):
            raise LabError("protocol_mismatch", manifest.batch)
        if self.instrument_state(instrument.key)["withdrawn_at"]:
            raise LabError("instrument_withdrawn", instrument.key)
        doc = manifest.document()
        digest = digest_document(doc)
        with self.transaction() as db:
            row = db.execute("SELECT digest FROM batches WHERE instrument=? AND batch=?",
                             (manifest.instrument, manifest.batch)).fetchone()
            if row and row["digest"] != digest:
                raise LabError("batch_manifest_conflict", manifest.batch)
            if not row:
                instant = now()
                db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?,?,?)",
                           (manifest.instrument, manifest.batch, canonical(doc), digest,
                            "pending", manifest.plate, instant, instant))
                for page in manifest.pages:
                    db.execute("INSERT INTO pages(instrument,batch,ordinal,path,format,expected_sha256,state) VALUES(?,?,?,?,?,?,?)",
                               (manifest.instrument, manifest.batch, page.ordinal, page.path, page.format,
                                page.sha256, "pending"))
                self._event(db, "batch_created", manifest.instrument, manifest.batch, {"digest": digest})
        return {"instrument": manifest.instrument, "batch": manifest.batch,
                "digest": digest, "created": not bool(row)}

    def page(self, instrument: str, batch: str, ordinal: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM pages WHERE instrument=? AND batch=? AND ordinal=?",
                                      (instrument, batch, ordinal)).fetchone()
        if not row:
            raise LabError("unknown_page", f"{instrument}/{batch}/{ordinal}")
        return dict(row)

    def _refresh_batch(self, db: sqlite3.Connection, instrument: str, batch: str) -> None:
        pages = [dict(row) for row in db.execute("SELECT state FROM pages WHERE instrument=? AND batch=?",
                                                (instrument, batch))]
        db.execute("UPDATE batches SET state=?,updated_at=? WHERE instrument=? AND batch=?",
                   (batch_state(pages), now(), instrument, batch))

    def fail_page(self, instrument: str, batch: str, ordinal: int, error: LabError) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute("SELECT state FROM pages WHERE instrument=? AND batch=? AND ordinal=?",
                             (instrument, batch, ordinal)).fetchone()
            if not row:
                raise LabError("unknown_page", str(ordinal))
            if row["state"] == "committed":
                return self.page(instrument, batch, ordinal)
            db.execute("UPDATE pages SET state='failed',error_code=?,error_detail=?,attempts=attempts+1 WHERE instrument=? AND batch=? AND ordinal=?",
                       (error.code, error.detail, instrument, batch, ordinal))
            self._refresh_batch(db, instrument, batch)
            self._event(db, "page_failed", instrument, batch, {"ordinal": ordinal, **error.document()})
        return self.page(instrument, batch, ordinal)

    def commit_page(self, instrument: str, batch: str, ordinal: int,
                    observations: list[Observation]) -> dict[str, Any]:
        with self.transaction() as db:
            page = db.execute("SELECT state FROM pages WHERE instrument=? AND batch=? AND ordinal=?",
                              (instrument, batch, ordinal)).fetchone()
            if not page:
                raise LabError("unknown_page", str(ordinal))
            if page["state"] == "committed":
                return self.page(instrument, batch, ordinal)
            for row in observations:
                db.execute("""INSERT INTO observations(instrument,batch,ordinal,row_number,plate,sample,well,
                           channel,value,unit,measured_at,identity_key,status,reason,raw_json,observed_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (row.instrument, row.batch, row.ordinal, row.row_number, row.plate,
                            row.sample, row.well, row.channel, row.value, row.unit, row.measured_at,
                            row.identity, row.status, row.reason, canonical(row.raw), now()))
            db.execute("UPDATE pages SET state='committed',error_code=NULL,error_detail=NULL,row_count=?,attempts=attempts+1,completed_at=? WHERE instrument=? AND batch=? AND ordinal=?",
                       (len(observations), now(), instrument, batch, ordinal))
            self._refresh_batch(db, instrument, batch)
            self._event(db, "page_committed", instrument, batch,
                        {"ordinal": ordinal, "rows": len(observations)})
        return self.page(instrument, batch, ordinal)

    def batch(self, instrument: str, batch: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM batches WHERE instrument=? AND batch=?",
                                      (instrument, batch)).fetchone()
        if not row:
            raise LabError("unknown_batch", f"{instrument}/{batch}")
        result = dict(row)
        result["manifest"] = json.loads(result["manifest"])
        result["pages"] = [dict(page) for page in self.connection.execute(
            "SELECT * FROM pages WHERE instrument=? AND batch=? ORDER BY ordinal", (instrument, batch))]
        return result

    def batches(self, instrument: str | None = None) -> list[dict[str, Any]]:
        if instrument:
            rows = self.connection.execute("SELECT instrument,batch FROM batches WHERE instrument=? ORDER BY created_at,batch",
                                           (instrument,)).fetchall()
        else:
            rows = self.connection.execute("SELECT instrument,batch FROM batches ORDER BY created_at,batch").fetchall()
        return [self.batch(row["instrument"], row["batch"]) for row in rows]

    def withdraw(self, instrument: str, reason: str) -> dict[str, Any]:
        if not reason or len(reason) > 240:
            raise LabError("invalid_reason", instrument)
        with self.transaction() as db:
            row = db.execute("SELECT withdrawn_at FROM instruments WHERE key=?", (instrument,)).fetchone()
            if not row:
                raise LabError("unknown_instrument", instrument)
            if not row["withdrawn_at"]:
                db.execute("UPDATE instruments SET withdrawn_at=?,withdrawal_reason=? WHERE key=?",
                           (now(), reason, instrument))
                self._event(db, "instrument_withdrawn", instrument, None, {"reason": reason})
        return self.instrument_state(instrument)

    def observations(self, *, instrument: str | None = None,
                     identity: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[str] = []
        if instrument:
            clauses.append("o.instrument=?")
            args.append(instrument)
        if identity:
            clauses.append("o.identity_key=?")
            args.append(identity)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute("""SELECT o.*,i.trust,i.withdrawn_at,i.withdrawal_reason,
                   b.digest AS manifest_digest,b.state AS batch_state
                   FROM observations o JOIN instruments i ON i.key=o.instrument
                   JOIN batches b ON b.instrument=o.instrument AND b.batch=o.batch"""
                   + where + " ORDER BY o.id", args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["raw"] = json.loads(item.pop("raw_json"))
            item["identity"] = item.pop("identity_key")
            item["withdrawn"] = bool(item["withdrawn_at"])
            result.append(item)
        return result

    def events(self, instrument: str | None = None) -> list[dict[str, Any]]:
        if instrument:
            rows = self.connection.execute("SELECT * FROM events WHERE instrument=? ORDER BY id", (instrument,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM events ORDER BY id").fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def record_snapshot(self, path: str, sha256: str, count: int) -> dict[str, Any]:
        with self.transaction() as db:
            cursor = db.execute("INSERT INTO snapshots(path,sha256,created_at,record_count) VALUES(?,?,?,?)",
                                (path, sha256, now(), count))
            self._event(db, "snapshot_created", None, None, {"id": cursor.lastrowid, "sha256": sha256})
        return {"id": cursor.lastrowid, "path": path, "sha256": sha256, "record_count": count}

    def snapshots(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM snapshots ORDER BY id")]
