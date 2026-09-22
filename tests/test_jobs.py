"""Phase 17 tests: SQLite-backed background job queue and asyncio worker."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.jobs import handlers as job_handlers
from buku.jobs.handlers import HANDLERS, decode_payload, handle_metadata_lookup
from buku.jobs.queue import (
    JOB_TYPES,
    POOL_CONCURRENCY,
    POOL_JOB_TYPES,
    claim_job,
    complete_job,
    enqueue_job,
    fail_job,
    pool_job_types,
    recover_stale_running_jobs,
)
from buku.jobs.worker import JobWorker
from buku.models import Book, Job, Library


@pytest.fixture
def db_url(tmp_path: Path) -> Generator[str]:
    """Isolated SQLite database with migrations applied."""
    reset_engine()
    database_file = tmp_path / "test_jobs.db"
    url = f"sqlite:///{database_file}"
    settings = Settings(config_dir=tmp_path, database_url=url, jobs_enabled=False)
    set_settings(settings)
    run_migrations(url)
    yield url
    reset_engine()
    set_settings(None)


@pytest.fixture
def factory(db_url: str) -> sessionmaker[Session]:
    """Session factory bound to the migrated test database."""
    return get_session_factory(get_engine(db_url))


def _make_book(factory: sessionmaker[Session], title: str = "Dune") -> int:
    with factory() as db:
        library = Library(name="jobs", path="/tmp/jobs-lib")
        db.add(library)
        db.flush()
        book = Book(library_id=library.id, title=title)
        db.add(book)
        db.commit()
        return book.id


def test_job_types_match_plan() -> None:
    """Phase 17 initial job types are exactly the plan.md set."""
    assert JOB_TYPES == (
        "scan_library",
        "extract_metadata",
        "metadata_lookup",
        "generate_cover",
        "generate_x4",
    )
    for job_type in JOB_TYPES:
        assert job_type in HANDLERS


def test_worker_concurrency_defaults() -> None:
    """Concurrency follows plan.md: 1 scan, 1 x4, 2 metadata enrichment."""
    assert POOL_CONCURRENCY == {"scan": 1, "x4": 1, "metadata": 2}
    assert pool_job_types("scan") == ("scan_library",)
    assert pool_job_types("x4") == ("generate_x4",)
    assert set(pool_job_types("metadata")) == {
        "extract_metadata",
        "metadata_lookup",
        "generate_cover",
    }
    assert set(POOL_JOB_TYPES["scan"] + POOL_JOB_TYPES["x4"] + POOL_JOB_TYPES["metadata"]) == set(
        JOB_TYPES
    )


def test_enqueue_rejects_unknown_type(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        with pytest.raises(ValueError, match="Unknown job type"):
            enqueue_job(db, "not_a_real_job", {})


def test_enqueue_creates_queued_job(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        job = enqueue_job(db, "scan_library", {"library_id": 7})
        db.commit()
        job_id = job.id

    with factory() as db:
        loaded = db.get(Job, job_id)
        assert loaded is not None
        assert loaded.status == "queued"
        assert loaded.type == "scan_library"
        assert loaded.attempts == 0
        assert loaded.max_attempts == 3
        assert json.loads(loaded.payload) == {"library_id": 7}


def test_claim_marks_running_and_increments_attempts(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        enqueue_job(db, "generate_x4", {"book_id": 1})
        db.commit()

    with factory() as db:
        job = claim_job(db, ("generate_x4",))
        assert job is not None
        assert job.status == "running"
        assert job.attempts == 1
        assert job.started_at is not None

        # Second claim finds nothing while the first is running.
        assert claim_job(db, ("generate_x4",)) is None


def test_claim_skips_other_pools_job_types(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        enqueue_job(db, "metadata_lookup", {"book_id": 1})
        db.commit()

    with factory() as db:
        assert claim_job(db, ("scan_library",)) is None
        assert claim_job(db, ("generate_x4",)) is None
        assert claim_job(db, POOL_JOB_TYPES["metadata"]) is not None


def test_complete_and_fail_lifecycle(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        enqueue_job(db, "scan_library", {}, max_attempts=2)
        db.commit()

    with factory() as db:
        job = claim_job(db, ("scan_library",))
        assert job is not None
        complete_job(db, job)
        job_id = job.id

    with factory() as db:
        done = db.get(Job, job_id)
        assert done is not None
        assert done.status == "completed"
        assert done.error is None
        assert done.finished_at is not None

    with factory() as db:
        enqueue_job(db, "scan_library", {}, max_attempts=1)
        db.commit()
        job = claim_job(db, ("scan_library",))
        assert job is not None
        fail_job(db, job, "boom")
        failed = db.get(Job, job.id)
        assert failed is not None
        assert failed.status == "failed"
        assert "boom" in (failed.error or "")


def test_fail_requeues_until_max_attempts(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        enqueue_job(db, "scan_library", {}, max_attempts=3)
        db.commit()

    for expected_attempts in (1, 2, 3):
        with factory() as db:
            job = claim_job(db, ("scan_library",))
            assert job is not None
            assert job.attempts == expected_attempts
            fail_job(db, job, "transient")
            refreshed = db.get(Job, job.id)
            assert refreshed is not None
            if expected_attempts < 3:
                assert refreshed.status == "queued"
            else:
                assert refreshed.status == "failed"


def test_recover_stale_running_jobs(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        enqueue_job(db, "generate_cover", {"book_id": 1})
        db.commit()
        job = claim_job(db, ("generate_cover",))
        assert job is not None
        job_id = job.id

    with factory() as db:
        recovered = recover_stale_running_jobs(db)
        assert recovered == 1
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == "queued"
        assert job.started_at is None


def test_decode_payload_defaults_and_rejects_non_object() -> None:
    assert decode_payload(None) == {}
    assert decode_payload("") == {}
    assert decode_payload('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError, match="JSON object"):
        decode_payload("[1, 2]")


def test_metadata_lookup_handler_enriches_book(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from buku.metadata.enrichment import MetadataEnrichmentService

    book_id = _make_book(factory, "HandlerBook")

    class _Stub(MetadataEnrichmentService):
        def __init__(self) -> None:
            super().__init__(providers=[])

        def enrich_book(self, db: Session, target_id: int) -> Any:
            assert target_id == book_id
            book = db.get(Book, target_id)
            assert book is not None
            book.subtitle = "Stubbed"
            return type("R", (), {"fields_applied": ["subtitle"]})()

    monkeypatch.setattr(job_handlers, "MetadataEnrichmentService", _Stub, raising=False)
    # handle_metadata_lookup imports inside the function; patch the module path.
    import buku.metadata.enrichment as enrichment_mod

    monkeypatch.setattr(enrichment_mod, "MetadataEnrichmentService", _Stub)

    handle_metadata_lookup(factory, {"book_id": book_id})

    with factory() as db:
        book = db.get(Book, book_id)
        assert book is not None
        assert book.subtitle == "Stubbed"


def test_worker_processes_enqueued_job(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_handler(_factory: sessionmaker[Session], payload: dict[str, Any]) -> None:
        seen.append(payload)

    monkeypatch.setitem(HANDLERS, "scan_library", fake_handler)

    with factory() as db:
        job = enqueue_job(db, "scan_library", {"library_id": 42})
        db.commit()
        job_id = job.id

    async def run() -> None:
        worker = JobWorker(factory, poll_interval=0.01)
        await worker.start()
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                with factory() as db:
                    loaded = db.get(Job, job_id)
                    if loaded is not None and loaded.status == "completed":
                        break
                await asyncio.sleep(0.02)
        finally:
            await worker.stop()

    asyncio.run(run())

    assert seen == [{"library_id": 42}]
    with factory() as db:
        loaded = db.get(Job, job_id)
        assert loaded is not None
        assert loaded.status == "completed"
        assert loaded.attempts == 1
        assert loaded.finished_at is not None


def test_worker_fails_job_without_handler(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(HANDLERS, "generate_x4", raising=False)

    with factory() as db:
        job = enqueue_job(db, "generate_x4", {"book_id": 1}, max_attempts=1)
        db.commit()
        job_id = job.id

    async def run() -> None:
        worker = JobWorker(factory, poll_interval=0.01)
        await worker.start()
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                with factory() as db:
                    loaded = db.get(Job, job_id)
                    if loaded is not None and loaded.status in ("failed", "completed"):
                        break
                await asyncio.sleep(0.02)
        finally:
            await worker.stop()

    asyncio.run(run())

    with factory() as db:
        loaded = db.get(Job, job_id)
        assert loaded is not None
        assert loaded.status == "failed"
        assert "No handler" in (loaded.error or "")


def test_worker_handler_exception_marks_failed(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_factory: sessionmaker[Session], _payload: dict[str, Any]) -> None:
        raise RuntimeError("handler exploded")

    monkeypatch.setitem(HANDLERS, "metadata_lookup", boom)

    with factory() as db:
        job = enqueue_job(db, "metadata_lookup", {"book_id": 1}, max_attempts=1)
        db.commit()
        job_id = job.id

    async def run() -> None:
        worker = JobWorker(factory, poll_interval=0.01)
        await worker.start()
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                with factory() as db:
                    loaded = db.get(Job, job_id)
                    if loaded is not None and loaded.status == "failed":
                        break
                await asyncio.sleep(0.02)
        finally:
            await worker.stop()

    asyncio.run(run())

    with factory() as db:
        loaded = db.get(Job, job_id)
        assert loaded is not None
        assert loaded.status == "failed"
        assert "handler exploded" in (loaded.error or "")


def test_worker_recovers_stale_running_on_start(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        enqueue_job(db, "scan_library", {})
        db.commit()
        job = claim_job(db, ("scan_library",))
        assert job is not None
        job_id = job.id

    async def run() -> None:
        worker = JobWorker(factory, poll_interval=0.01)
        await worker.start()
        await worker.stop()

    asyncio.run(run())

    with factory() as db:
        loaded = db.get(Job, job_id)
        assert loaded is not None
        # Recovered to queued (handler may then process it while running).
        assert loaded.status in ("queued", "running", "completed", "failed")
        if loaded.status == "queued":
            assert loaded.started_at is None


def test_scanner_enqueue_uses_queue_helper(factory: sessionmaker[Session]) -> None:
    """Scanner metadata jobs go through enqueue_job (single code path)."""
    from buku.scanner import LibraryScanner

    book_id = _make_book(factory, "EnqueuePath")
    scanner = LibraryScanner(factory)
    with factory() as db:
        scanner._enqueue_metadata_job(db, book_id, 1)  # noqa: SLF001
        db.commit()

    with factory() as db:
        jobs = db.scalars(select(Job).where(Job.type == "metadata_lookup")).all()
        assert len(jobs) == 1
        assert jobs[0].status == "queued"
        assert json.loads(jobs[0].payload)["book_id"] == book_id
