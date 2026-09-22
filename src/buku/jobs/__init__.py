"""SQLite-backed background job queue (Phase 17).

No Redis or Celery: jobs are rows in the ``jobs`` table, claimed and executed
by in-process asyncio worker pools started from the FastAPI lifespan.
"""

from buku.jobs.handlers import HANDLERS, JobHandler
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

__all__ = [
    "HANDLERS",
    "JOB_TYPES",
    "JobHandler",
    "JobWorker",
    "POOL_CONCURRENCY",
    "POOL_JOB_TYPES",
    "claim_job",
    "complete_job",
    "enqueue_job",
    "fail_job",
    "pool_job_types",
    "recover_stale_running_jobs",
]
