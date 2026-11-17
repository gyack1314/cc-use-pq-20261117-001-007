from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .audit import Auditor
from .checks import Readiness
from .compare import Comparator
from .contracts import LabError
from .ingest import Harvester
from .release import Delivery, verify_snapshot
from .report import Reporter
from .store import Store


class ManifestRequest(BaseModel):
    path: str = Field(min_length=1)
    stop_on_error: bool = False


class WithdrawalRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=240)


class SnapshotRequest(BaseModel):
    path: str = Field(min_length=1)
    include_excluded: bool = False


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="LabTrace", version="1.0")
    app.state.db_path = Path(db_path or os.environ.get("LABTRACE_DB", "labtrace.sqlite3"))

    def run(method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            with Store(app.state.db_path) as store:
                target = Harvester(store) if method.startswith("harvest.") else Delivery(store)
                name = method.partition(".")[2]
                return getattr(target, name)(*args, **kwargs)
        except LabError as exc:
            raise HTTPException(status_code=409 if "conflict" in exc.code else 422,
                                detail=exc.document()) from None

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/protocols")
    def protocol(declaration: dict[str, Any]) -> dict[str, Any]:
        return run("harvest.register_protocol", declaration)

    @app.post("/instruments")
    def instrument(declaration: dict[str, Any]) -> dict[str, Any]:
        return run("harvest.register_instrument", declaration)

    @app.post("/batches/ingest")
    def ingest(request: ManifestRequest) -> dict[str, Any]:
        return run("harvest.ingest", request.path, stop_on_error=request.stop_on_error)

    @app.post("/batches/resume")
    def resume(request: ManifestRequest) -> dict[str, Any]:
        return run("harvest.resume", request.path)

    @app.post("/batches/retry/{ordinal}")
    def retry(ordinal: int, request: ManifestRequest) -> dict[str, Any]:
        return run("harvest.retry_page", request.path, ordinal)

    @app.get("/batches/{instrument}/{batch}")
    def batch(instrument: str, batch: str) -> dict[str, Any]:
        return run("harvest.inspect", instrument, batch)

    @app.get("/batches/{instrument}/{batch}/pages/{ordinal}/quality")
    def page_quality(instrument: str, batch: str, ordinal: int) -> dict[str, Any]:
        return run("harvest.page_quality", instrument, batch, ordinal)

    @app.post("/instruments/{instrument}/withdraw")
    def withdraw(instrument: str, request: WithdrawalRequest) -> dict[str, Any]:
        return run("harvest.withdraw", instrument, request.reason)

    @app.get("/current")
    def current(include_excluded: bool = False) -> dict[str, Any]:
        return run("delivery.current", include_excluded=include_excluded)

    @app.get("/history")
    def history(instrument: str | None = None, identity: str | None = None) -> dict[str, Any]:
        return run("delivery.history", instrument=instrument, identity=identity)

    @app.get("/trace")
    def trace(identity: str = Query(min_length=1)) -> dict[str, Any]:
        return run("delivery.trace", identity)

    @app.get("/summary")
    def summary() -> dict[str, Any]:
        return run("delivery.summary")

    @app.get("/audit")
    def audit() -> dict[str, Any]:
        with Store(app.state.db_path) as store:
            return Auditor(store).run()

    @app.get("/readiness")
    def readiness() -> dict[str, Any]:
        with Store(app.state.db_path) as store:
            return Readiness(store).evaluate()

    @app.get("/reports/batch/{instrument}/{batch}")
    def batch_report(instrument: str, batch: str) -> dict[str, Any]:
        try:
            with Store(app.state.db_path) as store:
                return Reporter(store).batch_report(instrument, batch)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=exc.document()) from None

    @app.get("/reports/instrument/{instrument}")
    def instrument_report(instrument: str) -> dict[str, Any]:
        try:
            with Store(app.state.db_path) as store:
                return Reporter(store).instrument_report(instrument)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=exc.document()) from None

    @app.get("/reports/plate/{plate}")
    def plate_report(plate: str) -> dict[str, Any]:
        with Store(app.state.db_path) as store:
            return Reporter(store).plate_report(plate)

    @app.get("/reports/exceptions")
    def exception_report() -> dict[str, Any]:
        with Store(app.state.db_path) as store:
            return Reporter(store).exception_report()

    @app.get("/reports/coverage")
    def coverage_report() -> dict[str, Any]:
        with Store(app.state.db_path) as store:
            return Reporter(store).coverage_report()

    @app.get("/compare/snapshot/{snapshot_id}")
    def compare_snapshot(snapshot_id: int) -> dict[str, Any]:
        try:
            with Store(app.state.db_path) as store:
                return Comparator(store).snapshot_to_current(snapshot_id)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=exc.document()) from None

    @app.get("/compare/batch/{instrument}/{batch}")
    def compare_batch(instrument: str, batch: str) -> dict[str, Any]:
        try:
            with Store(app.state.db_path) as store:
                return Comparator(store).batch_to_current(instrument, batch)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=exc.document()) from None

    @app.get("/compare/instrument/{instrument}")
    def compare_instrument(instrument: str) -> dict[str, Any]:
        try:
            with Store(app.state.db_path) as store:
                return Comparator(store).source_contribution(instrument)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=exc.document()) from None

    @app.post("/snapshots")
    def snapshot(request: SnapshotRequest) -> dict[str, Any]:
        return run("delivery.write_snapshot", request.path,
                   include_excluded=request.include_excluded)

    @app.get("/snapshots/verify")
    def verify(path: str, sha256: str) -> dict[str, Any]:
        try:
            return verify_snapshot(path, sha256)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=exc.document()) from None

    return app
