"""Admin-facing read queries for the web UI (Phase 9).

Provides the lightweight read models behind the admin console pages — users,
libraries, and the SQLite-backed job queue. Mutations stay in the domain
services (auth, scanner, jobs); this service is strictly read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from buku.models.job import Job
from buku.models.library import Library
from buku.models.user import User

JOB_STATUSES = ("queued", "running", "completed", "failed")


@dataclass
class JobSummary:
    """Counts by status plus the most recent queue entries."""

    total: int = 0
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    recent: list[Job] = field(default_factory=list)


class AdminService:
    """Read-only admin queries backing console pages."""

    def list_users(self, db: Session) -> list[User]:
        """All user accounts, oldest first."""
        return list(db.scalars(select(User).order_by(User.id)).all())

    def list_libraries(self, db: Session) -> list[Library]:
        """All configured library roots."""
        return list(db.scalars(select(Library).order_by(Library.name)).all())

    def job_summary(self, db: Session, recent_limit: int = 50) -> JobSummary:
        """Aggregate job queue counts and the most recent jobs."""
        counts: dict[str, int] = {
            status: count
            for status, count in db.execute(
                select(Job.status, func.count()).group_by(Job.status)
            ).all()
        }
        recent = list(
            db.scalars(select(Job).order_by(Job.created_at.desc()).limit(recent_limit)).all()
        )
        return JobSummary(
            total=sum(counts.values()),
            queued=counts.get("queued", 0),
            running=counts.get("running", 0),
            completed=counts.get("completed", 0),
            failed=counts.get("failed", 0),
            recent=recent,
        )


admin_service = AdminService()
