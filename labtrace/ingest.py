from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import extract, map_fields
from .contracts import (Instrument, LabError, Manifest, Observation, Protocol,
                        digest_bytes)
from .measure import channel_for, measurement
from .quality import assess, validate_page_measurements
from .store import Store


@dataclass(frozen=True)
class PageResult:
    ordinal: int
    state: str
    rows: int
    error: dict[str, str] | None

    def document(self) -> dict[str, Any]:
        return {"ordinal": self.ordinal, "state": self.state,
                "rows": self.rows, "error": self.error}


class Harvester:
    def __init__(self, store: Store):
        self.store = store

    def register_protocol(self, declaration: dict[str, Any]) -> dict[str, Any]:
        return self.store.register_protocol(Protocol.parse(declaration))

    def register_instrument(self, declaration: dict[str, Any]) -> dict[str, Any]:
        return self.store.register_instrument(Instrument.parse(declaration))

    def load_manifest(self, path: str | Path) -> Manifest:
        candidate = Path(path)
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise LabError("manifest_missing", str(candidate)) from None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LabError("invalid_manifest", str(exc)) from None
        return Manifest.parse(value)

    def _compatible(self, manifest: Manifest, instrument: Instrument) -> Protocol:
        if (manifest.protocol_key, manifest.protocol_revision) != (
                instrument.protocol_key, instrument.protocol_revision):
            raise LabError("protocol_mismatch", manifest.batch)
        for page in manifest.pages:
            if page.format not in instrument.formats:
                raise LabError("unsupported_instrument_format", page.format)
        return self.store.protocol(manifest.protocol_key, manifest.protocol_revision)

    def ingest(self, manifest_path: str | Path, *, stop_on_error: bool = False,
               ordinals: set[int] | None = None) -> dict[str, Any]:
        path = Path(manifest_path)
        manifest = self.load_manifest(path)
        instrument = self.store.instrument(manifest.instrument)
        protocol = self._compatible(manifest, instrument)
        self.store.ensure_batch(manifest)
        results = []
        for page in manifest.pages:
            if ordinals is not None and page.ordinal not in ordinals:
                continue
            current = self.store.page(manifest.instrument, manifest.batch, page.ordinal)
            if current["state"] == "committed":
                results.append(PageResult(page.ordinal, "committed", current["row_count"], None).document())
                continue
            try:
                observations = self._process(path.parent, manifest, instrument, protocol, page.ordinal)
                committed = self.store.commit_page(manifest.instrument, manifest.batch,
                                                   page.ordinal, observations)
                results.append(PageResult(page.ordinal, "committed", committed["row_count"], None).document())
            except LabError as exc:
                failed = self.store.fail_page(manifest.instrument, manifest.batch, page.ordinal, exc)
                results.append(PageResult(page.ordinal, "failed", failed["row_count"], exc.document()).document())
                if stop_on_error:
                    break
        batch = self.store.batch(manifest.instrument, manifest.batch)
        return {"instrument": manifest.instrument, "batch": manifest.batch,
                "state": batch["state"], "pages": results}

    def _process(self, base: Path, manifest: Manifest, instrument: Instrument,
                 protocol: Protocol, ordinal: int) -> list[Observation]:
        page = next(item for item in manifest.pages if item.ordinal == ordinal)
        target = (base / page.path).resolve()
        if not target.is_relative_to(base.resolve()) or target.is_symlink():
            raise LabError("unsafe_page_path", page.path)
        try:
            content = target.read_bytes()
        except FileNotFoundError:
            raise LabError("page_missing", page.path) from None
        if digest_bytes(content) != page.sha256:
            raise LabError("page_digest_mismatch", page.path)
        rows = extract(content, page.format, limit=protocol.page_limit)
        output = []
        for number, raw in enumerate(rows, start=1):
            try:
                mapped = map_fields(raw, instrument.field_map, protocol.required_fields)
                channel = channel_for(protocol.channels, mapped.get("channel"))
                item = measurement(mapped, channel, manifest.plate)
                decision = assess(item, channel)
            except LabError as exc:
                raise LabError("row_invalid", f"row {number}: {exc}") from None
            output.append(Observation(instrument.key, manifest.batch, ordinal, number,
                                      manifest.plate, item["sample"], item["well"],
                                      item["channel"], item["value"], item["unit"],
                                      item["measured_at"], item["identity"], decision.status,
                                      decision.reason, raw))
        validate_page_measurements(output)
        return output

    def resume(self, manifest_path: str | Path) -> dict[str, Any]:
        manifest = self.load_manifest(manifest_path)
        try:
            batch = self.store.batch(manifest.instrument, manifest.batch)
        except LabError:
            return self.ingest(manifest_path)
        pending = {page["ordinal"] for page in batch["pages"] if page["state"] != "committed"}
        return self.ingest(manifest_path, ordinals=pending)

    def retry_page(self, manifest_path: str | Path, ordinal: int) -> dict[str, Any]:
        manifest = self.load_manifest(manifest_path)
        if ordinal not in {page.ordinal for page in manifest.pages}:
            raise LabError("unknown_page", str(ordinal))
        return self.ingest(manifest_path, ordinals={ordinal})

    def withdraw(self, instrument: str, reason: str) -> dict[str, Any]:
        return self.store.withdraw(instrument, reason)

    def inspect(self, instrument: str, batch: str) -> dict[str, Any]:
        return self.store.batch(instrument, batch)

    def page_quality(self, instrument: str, batch: str, ordinal: int) -> dict[str, Any]:
        page = self.store.page(instrument, batch, ordinal)
        rows = [item for item in self.store.observations(instrument=instrument)
                if item["batch"] == batch and item["ordinal"] == ordinal]
        counts: dict[str, int] = {}
        for item in rows:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {"page": page, "counts": counts,
                "row_count_matches": len(rows) == page["row_count"]}
