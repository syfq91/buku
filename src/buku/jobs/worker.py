"""In-process asyncio worker pools for the SQLite job queue (Phase 17).

Each pool claims only its own job types so concurrency limits map cleanly to
plan.md defaults (1 library scan, 1 X4 generation, 2 metadata enrichment).
Handler execution runs in worker threads via ``asyncio.to_thread`` so the
event loop stays responsive while scans and X4 builds are CPU/IO heavy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from sqlalchemy.orm import Session, sessionmaker

from buku.jobs.handlers import HANDLERS, decode_payload
from buku.jobs.queue import (
    POOL_CONCURRENCY,
    POOL_JOB_TYPES,
    claim_job,
    complete_job,
    fail_job,
    recover_stale_running_jobs,
)
from buku.models.job import Job

logger = logging.getLogger("buku.jobs.worker")


class JobWorker:
    """Polls the jobs table and executes work in per-pool asyncio tasks."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        poll_interval: float = 1.0,
    ) -> None:
        self.session_factory = session_factory
        self.poll_interval = max(poll_interval, 0.01)
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def running(self) -> bool:
        """Whether the worker pools are currently active."""
        return self._running

    async def start(self) -> None:
        """Recover stale jobs and spawn one asyncio task per pool slot."""
        if self._running:
            return
        self._running = True
        with self.session_factory() as db:
            recover_stale_running_jobs(db)
        for pool_name, concurrency in POOL_CONCURRENCY.items():
            job_types = POOL_JOB_TYPES.get(pool_name, ())
            for index in range(concurrency):
                task = asyncio.create_task(
                    self._pool_loop(f"{pool_name}-{index}", job_types),
                    name=f"buku-job-{pool_name}-{index}",
                )
                self._tasks.append(task)
        logger.info(
            "Job worker started (%d task(s), poll_interval=%.2fs)",
            len(self._tasks),
            self.poll_interval,
        )

    async def stop(self) -> None:
        """Cancel all pool tasks and wait for them to unwind."""
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Job worker stopped")

    async def _pool_loop(self, name: str, job_types: Sequence[str]) -> None:
        while self._running:
            try:
                processed = await asyncio.to_thread(self._drain_once, job_types)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Job pool %s crashed on an iteration", name)
                processed = False
            if not processed:
                try:
                    await asyncio.sleep(self.poll_interval)
                except asyncio.CancelledError:
                    raise

    def _drain_once(self, job_types: Sequence[str]) -> bool:
        """Claim and execute a single job. Returns True when one was handled."""
        with self.session_factory() as db:
            job = claim_job(db, job_types)
            if job is None:
                return False
            job_id = job.id
            job_type = job.type

        handler = HANDLERS.get(job_type)
        if handler is None:
            self._fail_by_id(job_id, f"No handler registered for job type {job_type!r}.")
            return True

        try:
            with self.session_factory() as db:
                job = db.get(Job, job_id)
                if job is None:
                    return True
                payload = decode_payload(job.payload)
            handler(self.session_factory, payload)
        except Exception as exc:
            logger.warning("Job %s (%s) failed: %s", job_id, job_type, exc)
            self._fail_by_id(job_id, str(exc))
            return True

        with self.session_factory() as db:
            job = db.get(Job, job_id)
            if job is not None:
                complete_job(db, job)
        logger.info("Job %s (%s) completed", job_id, job_type)
        return True

    def _fail_by_id(self, job_id: str, error: str) -> None:
        with self.session_factory() as db:
            job = db.get(Job, job_id)
            if job is not None:
                fail_job(db, job, error)
