"""Phase 13 tests: OPDS Progression 1.0 (RFC 7807 problem details).

Covers the wire document shape (``title``/``modified``/``device``/
``progression``/``references``), GET with empty-payload ``204``, PUT
create/update status codes, the ``409`` conflict with registry problem
details, ``400`` invalid-payload handling, user isolation, and the
locator (``href``/``fragment``) round-trip through ``references``.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import OpdsEnv
from tests.opds_helpers import basic_auth, seed_book

PROGRESSION = "application/opds-progression+json"
ERROR_DATE = "https://registry.opds.io/error#progression-date"
ERROR_PAYLOAD = "https://registry.opds.io/error#progression-invalid-payload"


def _document(progression: float, modified: str) -> dict[str, object]:
    """Build a minimal valid progression PUT body."""
    return {
        "modified": modified,
        "device": {"id": "kobo-1", "name": "Kobo Clara"},
        "progression": progression,
    }


# --------------------------------------------------------------------------- #
# GET /opds/progression/{book_id}
# --------------------------------------------------------------------------- #
def test_get_progression_requires_auth(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    client.cookies.clear()
    response = client.get(f"/opds/progression/{book_id}")
    assert response.status_code == 401


def test_get_progression_unknown_book_404(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    response = client.get(
        "/opds/progression/99999", headers={"Authorization": f"Bearer {reader_token}"}
    )
    assert response.status_code == 404


def test_get_progression_empty_payload_is_204(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    response = client.get(
        f"/opds/progression/{book_id}", headers={"Authorization": f"Bearer {reader_token}"}
    )
    assert response.status_code == 204
    assert response.content == b""


def test_get_progression_returns_document(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    put = client.put(
        f"/opds/progression/{book_id}",
        json={
            "modified": "2026-09-20T12:00:00Z",
            "device": {"id": "kobo-1", "name": "Kobo Clara"},
            "progression": 0.25,
            "references": ["chapter03.xhtml#p42"],
        },
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert put.status_code == 201
    get = client.get(
        f"/opds/progression/{book_id}", headers={"Authorization": f"Bearer {reader_token}"}
    )
    assert get.status_code == 200
    document = get.json()
    assert document["modified"] == "2026-09-20T12:00:00Z"
    assert document["device"] == {"id": "kobo-1", "name": "Kobo Clara"}
    assert document["progression"] == 0.25
    assert document["references"] == ["chapter03.xhtml#p42"]


# --------------------------------------------------------------------------- #
# PUT /opds/progression/{book_id}
# --------------------------------------------------------------------------- #
def test_put_progression_requires_auth(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    client.cookies.clear()
    response = client.put(
        f"/opds/progression/{book_id}", json=_document(0.5, "2026-09-20T12:00:00Z")
    )
    assert response.status_code == 401


def test_put_progression_unknown_book_404(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    response = client.put(
        "/opds/progression/99999",
        json=_document(0.5, "2026-09-20T12:00:00Z"),
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert response.status_code == 404


def test_put_progression_create_201(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    response = client.put(
        f"/opds/progression/{book_id}",
        json={
            "title": "Chapter 3",
            "modified": "2026-09-20T12:00:00Z",
            "device": {"id": "kobo-1", "name": "Kobo Clara"},
            "progression": 0.25,
            "references": ["chapter03.xhtml#p42"],
        },
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert response.status_code == 201
    assert response.headers["content-type"].startswith(PROGRESSION)
    body = response.json()
    assert body["title"] == "Chapter 3"
    assert body["progression"] == 0.25


def test_put_progression_update_200(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    client.put(
        f"/opds/progression/{book_id}",
        json=_document(0.25, "2026-09-20T12:00:00Z"),
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    newer = client.put(
        f"/opds/progression/{book_id}",
        json=_document(0.75, "2026-09-20T12:30:00Z"),
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert newer.status_code == 200
    assert newer.json()["progression"] == 0.75


def test_put_progression_stale_timestamp_conflicts_409(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    client.put(
        f"/opds/progression/{book_id}",
        json=_document(0.9, "2026-09-20T12:00:00Z"),
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    stale = client.put(
        f"/opds/progression/{book_id}",
        json=_document(0.1, "2026-09-20T11:00:00Z"),
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert stale.status_code == 409
    assert stale.headers["content-type"].startswith(PROGRESSION)
    body = stale.json()
    assert body["type"] == ERROR_DATE
    assert body["title"]
    detail = body["detail"]
    assert detail["stored"]["progression"] == 0.9
    assert detail["incoming"]["progression"] == 0.1
    # Canonical store must remain untouched.
    get = client.get(
        f"/opds/progression/{book_id}", headers={"Authorization": f"Bearer {reader_token}"}
    )
    assert get.json()["progression"] == 0.9


def test_put_progression_invalid_payload_400(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    for bad_payload in (
        {},  # missing required fields
        {"ended": True},  # wrong shape entirely
        {
            "modified": "2026-09-20T12:00:00Z",
            "device": {"id": "d", "name": "n"},
            "progression": 1.5,  # out of range
        },
    ):
        response = client.put(
            f"/opds/progression/{book_id}",
            json=bad_payload,
            headers={"Authorization": f"Bearer {reader_token}"},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["type"] == ERROR_PAYLOAD
        assert "detail" in body


def test_put_progression_works_with_http_basic(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    response = client.put(
        f"/opds/progression/{book_id}",
        json=_document(0.4, "2026-09-20T12:00:00Z"),
        headers=basic_auth("reader", "readerpass123"),
    )
    assert response.status_code == 201


# --------------------------------------------------------------------------- #
# Rule 3: user isolation
# --------------------------------------------------------------------------- #
def test_progression_is_scoped_to_user(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, admin_token = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    client.put(
        f"/opds/progression/{book_id}",
        json=_document(0.5, "2026-09-20T12:00:00Z"),
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    reader = client.get(
        f"/opds/progression/{book_id}", headers={"Authorization": f"Bearer {reader_token}"}
    )
    assert reader.json()["progression"] == 0.5
    admin = client.get(
        f"/opds/progression/{book_id}", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert admin.status_code == 204


# --------------------------------------------------------------------------- #
# Locator round-trip
# --------------------------------------------------------------------------- #
def test_references_round_trips_to_href_and_fragment(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    client.put(
        f"/opds/progression/{book_id}",
        json=_document(0.75, "2026-09-20T12:00:00Z"),
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    keepalive = client.put(
        f"/opds/progression/{book_id}",
        json={
            "modified": "2026-09-20T12:30:00Z",
            "device": {"id": "web", "name": "Web Reader"},
            "progression": 0.8,
            "references": ["chapter09.xhtml#p199"],
        },
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert keepalive.status_code == 200
    fetched = client.get(
        f"/opds/progression/{book_id}", headers={"Authorization": f"Bearer {reader_token}"}
    ).json()
    assert fetched["references"] == ["chapter09.xhtml#p199"]
