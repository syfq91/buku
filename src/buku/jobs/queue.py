"""Queue operations for the SQLite-backed job table (Phase 17).

Claiming uses an optimistic ``UPDATE ... WHERE status='queued'`` so multiple
in-process worker tasks cannot steal the same row, even under WAL concurrency.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from buku.models.base import utc_now
from buku.models.job import Job

logger = logging.getLogger("buku.jobs")

# All registered job types (plan.md Phases 17–18).
JOB_TYPES: tuple[str, ...] = (
    "scan_library",
    "extract_metadata",
    "metadata_lookup",
    "generate_cover",
    "generate_x4",
    "cache_maintenance",
)

# Worker pool name -> job types handled exclusively by that pool.
POOL_JOB_TYPES: dict[str, tuple[str, ...]] = {
    "scan": ("scan_library",),
    "x4": ("generate_x4",),
    "metadata": (
        "extract_metadata",
        "metadata_lookup",
        "generate_cover",
        "cache_maintenance",
    ),
}

# plan.md concurrency defaults: 1 scan, 1 x4, 2 metadata enrichment.
POOL_CONCURRENCY: dict[str, int] = {
    "scan": 1,
    "x4": 1,
    "metadata": 2,
}


def pool_job_types(pool_name: str) -> tuple[str, ...]:
    """Return the job types belonging to a named worker pool."""
    return POOL_JOB_TYPES.get(pool_name, ())


def enqueue_job(
    db: Session,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    max_attempts: int = 3,
) -> Job:
    """Insert a queued job row. Caller is responsible for committing."""
    if job_type not in JOB_TYPES:
        raise ValueError(f"Unknown job type: {job_type!r}")
    job = Job(
        id=str(uuid.uuid4()),
        type=job_type,
        status="queued",
        payload=json.dumps(payload or {}),
        attempts=0,
        max_attempts=max_attempts,
    )
    db.add(job)
    db.flush()
    return job


def claim_job(db: Session, job_types: Sequence[str]) -> Job | None:
    """Atomically claim the oldest queued job of the given types.

    Increments ``attempts`` and marks the row ``running``. Returns ``None``
    when nothing is available or another worker won the race.
    """
    if not job_types:
        return None
    candidate = db.scalar(
        select(Job)
        .where(Job.status == "queued", Job.type.in_(list(job_types)))
        .order_by(Job.created_at, Job.id)
        .limit(1)
    )
    if candidate is None:
        return None
    result = db.execute(
        update(Job)
        .where(Job.id == candidate.id, Job.status == "queued")
        .values(
            status="running",
            started_at=utc_now(),
            attempts=Job.attempts + 1,
        )
    )
    db.commit()
    if cast(CursorResult[Any], result).rowcount != 1:
        return None
    db.refresh(candidate)
    return candidate


def complete_job(db: Session, job: Job) -> None:
    """Mark a job successfully finished. Caller commits via this helper."""
    job.status = "completed"
    job.error = None
    job.finished_at = utc_now()
    db.commit()


def fail_job(db: Session, job: Job, error: str) -> None:
    """Record a failure; re-queue until ``max_attempts`` is exhausted."""
    job.error = error[:2000]
    job.finished_at = utc_now()
    if (job.attempts or 0) >= (job.max_attempts or 3):
        job.status = "failed"
        logger.warning("Job %s (%s) permanently failed: %s", job.id, job.type, error)
    else:
        job.status = "queued"
        job.started_at = None
        job.finished_at = None
        logger.info(
            "Job %s (%s) re-queued (attempt %s/%s): %s",
            job.id,
            job.type,
            job.attempts,
            job.max_attempts,
            error,
        )
    db.commit()


def recover_stale_running_jobs(db: Session) -> int:
    """Re-queue jobs stuck in ``running`` after a crash or hard restart."""
    stale = list(db.scalars(select(Job).where(Job.status == "running")).all())
    for job in stale:
        job.status = "queued"
        job.started_at = None
        job.finished_at = None
    if stale:
        db.commit()
        logger.warning("Re-queued %d stale running job(s)", len(stale))
    return len(stale)
