"""Representation profile abstraction (Phase 14).

A **profile** is a named, renderable form of a logical book — ``original``
(the book's own media files), ``x4`` (the e-ink-optimized EPUB), and any
future profiles. Profiles stay independent of the persistence layer; the
:class:`RepresentationService` in ``buku.services.representation`` maps them
onto the ``representations`` table and the writable cache under
``/config/cache/{profile}/``.

Only pure reads and writes to the writable cache happen here — profiles never
touch the read-only media directories (AGENTS.md Rule 1).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from buku.models.book import Book, BookFile


class RepresentationError(ValueError):
    """Base error for representation resolution or generation failures."""


class UnknownProfileError(RepresentationError):
    """Raised when a requested profile name is not registered."""


class ProfileNotSupportedError(RepresentationError):
    """Raised when a profile cannot be generated on demand (e.g. identity)."""


@dataclass(frozen=True)
class GenerationResult:
    """Outcome of rendering a profile into a cache target path."""

    format: str
    source_hash: str
    optimizer_version: str
    file_size_bytes: int


class RepresentationProfile(ABC):
    """A named, renderable profile of a logical book."""

    name: str
    format: str

    @abstractmethod
    def source_files(self, book: Book) -> list[BookFile]:
        """Real book files this profile derives from (missing files skipped)."""

    def exists(self, book: Book) -> bool:
        """Whether the book can be represented in this profile right now."""
        return any(self.source_files(book))

    def optimizer_version(self) -> str:
        """Version tag mixed into the cache key for this profile's generator."""
        return "1"

    @abstractmethod
    def generate(self, source: BookFile, target: Path) -> GenerationResult:
        """Render the representation from *source* into *target*.

        *target* is guaranteed to live inside the writable cache directory;
        media files are never read-write or modified.
        """


_PROFILES: dict[str, RepresentationProfile] = {}


def register_profile(profile: RepresentationProfile) -> None:
    """Register a profile under its ``name`` (replacing any previous one)."""
    _PROFILES[profile.name] = profile


def get_profile(name: str) -> RepresentationProfile:
    """Return the registered profile or raise :class:`UnknownProfileError`."""
    try:
        return _PROFILES[name]
    except KeyError:
        raise UnknownProfileError(f"Unknown representation profile: {name}") from None


def profile_names() -> list[str]:
    """Names of all registered profiles, sorted."""
    return sorted(_PROFILES)


__all__ = [
    "GenerationResult",
    "ProfileNotSupportedError",
    "RepresentationError",
    "RepresentationProfile",
    "UnknownProfileError",
    "get_profile",
    "profile_names",
    "register_profile",
]
