"""OPDS Progression 1.0 document adapter (Phase 13).

Implements the reading-state wire format defined by the OPDS Progression 1.0
draft: a JSON document with ``title``, ``modified``, ``device`` (``id`` +
``name``), ``progression`` and optional ``references``. The adapter:

- serializes a :class:`buku.services.progression.ProgressState` into a
  progression document (locators ``href``/``fragment`` are merged into the
  friendly ``references`` array),
- parses and validates an incoming document from a ``PUT`` body (raising
  ``ValueError`` on invalid payloads, surfaced as HTTP 400),
- emits RFC 7807 problem-details bodies for the error codes registry.

All state transitions still go through the single canonical
``ProgressionService`` — this module only shapes the messages on the wire.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from buku.services.progression import ProgressState

PROGRESSION_ERROR_DATE = "progression-date"
PROGRESSION_ERROR_PAYLOAD = "progression-invalid-payload"

_SERVER_DEVICE_ID = "buku://server"
_SERVER_DEVICE_NAME = "buku"


class _DeviceObject(BaseModel):
    """The ``device`` object required by every progression document."""

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)


class _ProgressionPayload(BaseModel):
    """The incoming ``PUT`` body validated against the Progression 1.0 draft."""

    title: str | None = Field(default=None, max_length=500)
    modified: datetime
    device: _DeviceObject
    progression: float = Field(ge=0.0, le=1.0)
    references: list[str] = Field(default_factory=list, max_length=50)


@dataclass(frozen=True)
class ProgressionDocument:
    """An OPDS Progression 1.0 document model."""

    title: str | None
    modified: datetime
    device_id: str
    device_name: str
    progression: float
    references: list[str]


def state_to_document(state: ProgressState) -> ProgressionDocument:
    """Build a progression document from the canonical stored state.

    The stored locator ``href``/``fragment`` pair maps onto the first
    ``references`` entry (``chapter03.xhtml#p42``), which is the most common
    way readers express a position inside a packaged publication.
    """
    references: list[str] = []
    if state.href:
        references.append(state.href + (f"#{state.fragment}" if state.fragment else ""))
    elif state.fragment:
        references.append(f"#{state.fragment}")
    return ProgressionDocument(
        title=state.title,
        modified=_as_utc(state.modified_at),
        device_id=state.device_id or _SERVER_DEVICE_ID,
        device_name=state.device_name or _SERVER_DEVICE_NAME,
        progression=state.progression,
        references=references,
    )


def document_from_payload(payload: Any) -> ProgressionDocument:
    """Parse and validate a raw ``PUT`` body into a progression document.

    Raises :class:`ValueError` with a readable reason on any malformed field
    so the transport layer can answer HTTP 400 with problem details.
    """
    try:
        parsed = _ProgressionPayload.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(_describe_validation_error(exc)) from exc
    return ProgressionDocument(
        title=parsed.title,
        modified=parsed.modified,
        device_id=parsed.device.id,
        device_name=parsed.device.name,
        progression=parsed.progression,
        references=list(parsed.references),
    )


def document_to_json(document: ProgressionDocument) -> dict[str, Any]:
    """Serialize a progression document to its JSON wire shape."""
    body: dict[str, Any] = {
        "modified": _format_instant(document.modified),
        "device": {"id": document.device_id, "name": document.device_name},
        "progression": document.progression,
    }
    if document.title:
        body["title"] = document.title
    if document.references:
        body["references"] = list(document.references)
    return body


def locator_from_references(references: Sequence[str]) -> tuple[str | None, str | None]:
    """Derive the stored ``href``/``fragment`` pair from the first reference.

    A reference like ``chapter03.xhtml#p42`` yields (``chapter03.xhtml``,
    ``p42``); a media fragment such as ``#page=6`` yields (``None``,
    ``page=6``) because it has no document path.
    """
    for reference in references:
        if not reference:
            continue
        path, _, fragment = reference.partition("#")
        return (path or None, fragment.partition("?")[0] or None)
    return None, None


def problem_details(
    registry_suffix: str,
    title: str,
    *,
    detail: Any = None,
) -> dict[str, Any]:
    """Build an RFC 7807 problem-details body from the OPDS errors registry."""
    body: dict[str, Any] = {
        "type": f"https://registry.opds.io/error#{registry_suffix}",
        "title": title,
    }
    if detail is not None:
        body["detail"] = detail
    return body


def _describe_validation_error(exc: ValidationError) -> str:
    """Compact the first Pydantic validation error into a readable reason."""
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"{location}: {first.get('msg', 'invalid value')}"


def _as_utc(value: datetime) -> datetime:
    """Normalize a (possibly naive) timestamp to an aware UTC instant."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_instant(value: datetime) -> str:
    """Format an instant as ISO 8601 in UTC with ``Z`` suffix (second precision)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "PROGRESSION_ERROR_DATE",
    "PROGRESSION_ERROR_PAYLOAD",
    "ProgressionDocument",
    "document_from_payload",
    "document_to_json",
    "locator_from_references",
    "problem_details",
    "state_to_document",
]
