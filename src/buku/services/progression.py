"""Central reading-progression service (Phase 10).

The single authority for reading progress state in buku. Progression belongs
strictly to the **logical Book** under the composite key ``(user_id,
book_id)`` — never to a specific EPUB/X4 representation — so the web reader
(Phase 11) and external e-reader sync (Phase 13) both converge on this
service and the same ``reading_progress`` rows.

Conflict handling
-----------------
Every progress record carries a ``modified_at`` UTC timestamp. When a sync
client submits an update that is **strictly older** than the stored
timestamp, the service refuses it and reports ``CONFLICT`` (surfaced as HTTP
409 by the transport layer). Equal timestamps and newer updates are applied.
Incoming timestamps are normalized to aware UTC before comparison because
SQLite does not persist timezone information.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from buku.models.book import Book, BookAuthor
from buku.models.progress import ReadingProgress

_MAX_HREF = 500
_MAX_FRAGMENT = 500
_MAX_TITLE = 500
_MAX_DEVICE_ID = 100
_MAX_DEVICE_NAME = 100


class ProgressionStatus(StrEnum):
    """Outcome of a progression write."""

    CREATED = "created"  # first progress record for the (user, book) pair
    UPDATED = "updated"  # newer (or equal) timestamp applied to existing row
    CONFLICT = "conflict"  # incoming timestamp strictly older than stored


@dataclass(frozen=True)
class ProgressState:
    """Immutable snapshot of a reading-progress record."""

    user_id: int
    book_id: int
    progression: float
    href: str | None
    fragment: str | None
    title: str | None
    modified_at: datetime
    device_id: str | None
    device_name: str | None


@dataclass(frozen=True)
class ProgressUpdateResult:
    """Result of a progression write: outcome plus snapshots.

    On ``CREATED`` / ``UPDATED``, ``state`` holds the applied values and
    ``previous`` the state before the write (``None`` on first creation).
    On ``CONFLICT``, ``state`` holds the rejected incoming values and
    ``previous`` the currently stored state.
    """

    status: ProgressionStatus
    state: ProgressState
    previous: ProgressState | None = None


class ProgressionService:
    """Read and write canonical reading-progression state."""

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get(self, db: Session, user_id: int, book_id: int) -> ReadingProgress | None:
        """Return the user's progress row for a book (canonical read)."""
        return db.scalar(
            select(ReadingProgress).where(
                ReadingProgress.user_id == user_id, ReadingProgress.book_id == book_id
            )
        )

    def get_state(self, db: Session, user_id: int, book_id: int) -> ProgressState | None:
        """Snapshot the user's progress for a book, or None when unrecorded."""
        record = self.get(db, user_id, book_id)
        return self._snapshot(record) if record is not None else None

    def list_recent(
        self, db: Session, user_id: int, limit: int = 8
    ) -> list[tuple[ReadingProgress, Book]]:
        """Return the user's most recently modified progress with books.

        The book is eagerly loaded with its display relationships for the
        "continue reading" shelf. Non-existent books are skipped so a moved
        or missing title never breaks the page.
        """
        rows = list(
            db.scalars(
                select(ReadingProgress)
                .where(ReadingProgress.user_id == user_id)
                .order_by(ReadingProgress.modified_at.desc())
                .limit(limit)
                .options(selectinload(ReadingProgress.book).options(*_BOOK_LOADS))
            ).all()
        )
        return [(row, row.book) for row in rows if row.book is not None]

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def update(
        self,
        db: Session,
        user_id: int,
        book_id: int,
        *,
        progression: float,
        href: str | None = None,
        fragment: str | None = None,
        title: str | None = None,
        modified_at: datetime | None = None,
        device_id: str | None = None,
        device_name: str | None = None,
    ) -> ProgressUpdateResult:
        """Create or update the user's progress for a logical book.

        Conflict rule (Rule 3 invariant): an incoming timestamp **older**
        than the stored timestamp is rejected with ``CONFLICT``; newer or
        equal timestamps are applied. The stored ``modified_at`` becomes the
        incoming timestamp so that timestamps behave monotonically across
        sync clients.
        """
        incoming_at = _as_utc(modified_at) if modified_at is not None else utc_now()
        if not math.isfinite(progression):
            raise ValueError("progression must be a finite number.")
        previous: ProgressState | None = None

        record = self.get(db, user_id, book_id)
        if record is None:
            record = ReadingProgress(
                user_id=user_id,
                book_id=book_id,
                progression=_clamp(progression),
                href=_truncate(href, _MAX_HREF),
                fragment=_truncate(fragment, _MAX_FRAGMENT),
                title=_truncate(title, _MAX_TITLE),
                modified_at=incoming_at,
                device_id=_truncate(device_id, _MAX_DEVICE_ID),
                device_name=_truncate(device_name, _MAX_DEVICE_NAME),
            )
            db.add(record)
            db.commit()
            return ProgressUpdateResult(
                status=ProgressionStatus.CREATED, state=self._snapshot(record)
            )

        previous = self._snapshot(record)
        if incoming_at < _as_utc(previous.modified_at):
            incoming = _state_from_values(
                record, incoming_at, progression, href, fragment, title, device_id, device_name
            )
            return ProgressUpdateResult(
                status=ProgressionStatus.CONFLICT, state=incoming, previous=previous
            )

        record.progression = _clamp(progression)
        record.href = _truncate(href, _MAX_HREF)
        record.fragment = _truncate(fragment, _MAX_FRAGMENT)
        record.title = _truncate(title, _MAX_TITLE)
        record.modified_at = incoming_at
        record.device_id = _truncate(device_id, _MAX_DEVICE_ID)
        record.device_name = _truncate(device_name, _MAX_DEVICE_NAME)
        db.commit()
        return ProgressUpdateResult(
            status=ProgressionStatus.UPDATED,
            state=self._snapshot(record),
            previous=previous,
        )

    def book_exists(self, db: Session, book_id: int) -> bool:
        """Return whether a logical book exists (path-param validation)."""
        return db.get(Book, book_id) is not None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _snapshot(self, record: ReadingProgress) -> ProgressState:
        """Capture the current column values of a progress row."""
        return _state_from_values(
            record,
            _as_utc(record.modified_at),
            record.progression,
            record.href,
            record.fragment,
            record.title,
            record.device_id,
            record.device_name,
        )


_BOOK_LOADS = (
    selectinload(Book.author_links).selectinload(BookAuthor.author),
    selectinload(Book.series),
    selectinload(Book.identifiers),
    selectinload(Book.files),
)


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime to an aware UTC instant.

    SQLite persists ``DateTime(timezone=True)`` without a timezone, so stored
    values come back naive; they are assumed to be UTC and given an explicit
    zone so comparisons against client-supplied aware timestamps are safe.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_now() -> datetime:
    """Return the current UTC instant (aware)."""
    return datetime.now(UTC)


def _clamp(value: float) -> float:
    """Constrain progression to the canonical ``[0.0, 1.0]`` range."""
    return min(1.0, max(0.0, value))


def _truncate(value: str | None, limit: int) -> str | None:
    """Guard long payloads before they reach the fixed-width columns."""
    return value[:limit] if value is not None else None


def _state_from_values(
    record: ReadingProgress,
    modified_at: datetime,
    progression: float,
    href: str | None,
    fragment: str | None,
    title: str | None,
    device_id: str | None,
    device_name: str | None,
) -> ProgressState:
    """Build an immutable snapshot for a row with (maybe unapplied) values."""
    return ProgressState(
        user_id=record.user_id,
        book_id=record.book_id,
        progression=_clamp(progression),
        href=href,
        fragment=fragment,
        title=title,
        modified_at=modified_at,
        device_id=device_id,
        device_name=device_name,
    )


progression_service = ProgressionService()


__all__ = [
    "ProgressState",
    "ProgressUpdateResult",
    "ProgressionService",
    "ProgressionStatus",
    "progression_service",
]
