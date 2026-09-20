"""Tests for Phase 3: Authentication, multi-user sessions, and authorization."""

from collections.abc import Generator
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from buku.app import create_app
from buku.cli import cli
from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.models import Book, Library, ReadingProgress, User
from buku.services.auth import auth_service
from buku.services.authorization import authorization_service


@pytest.fixture
def auth_test_env(tmp_path: Path) -> Generator[tuple[TestClient, str]]:
    """Set up migrated SQLite database and FastAPI TestClient."""
    reset_engine()
    db_path = tmp_path / "auth_test.db"
    db_url = f"sqlite:///{db_path}"
    settings = Settings(config_dir=tmp_path, database_url=db_url)
    set_settings(settings)

    run_migrations(db_url)

    app = create_app(settings)
    with TestClient(app) as client:
        yield client, db_url

    reset_engine()
    set_settings(None)


def test_two_users_can_login_simultaneously(auth_test_env: tuple[TestClient, str]) -> None:
    """Acceptance criterion: Two users can log in simultaneously with separate active sessions."""
    client, db_url = auth_test_env
    engine = get_engine(db_url)
    factory = get_session_factory(engine)

    # Create Alice (regular) and Bob (regular)
    with factory() as db:
        auth_service.create_user(db, "alice", "alicepass123", "Alice Reader")
        auth_service.create_user(db, "bob", "bobpass123", "Bob Reader")

    # Alice logs in
    resp_alice = client.post("/login", json={"username": "alice", "password": "alicepass123"})
    assert resp_alice.status_code == 200
    alice_token = resp_alice.json()["token"]
    assert "buku_session" in resp_alice.cookies

    # Bob logs in
    resp_bob = client.post("/login", json={"username": "bob", "password": "bobpass123"})
    assert resp_bob.status_code == 200
    bob_token = resp_bob.json()["token"]

    # Verify distinct sessions
    assert alice_token != bob_token

    # Verify Alice can access /me with her bearer token
    me_alice = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {alice_token}"})
    assert me_alice.status_code == 200
    assert me_alice.json()["username"] == "alice"

    # Verify Bob can access /me with his bearer token simultaneously
    me_bob = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bob_token}"})
    assert me_bob.status_code == 200
    assert me_bob.json()["username"] == "bob"


def test_user_cannot_access_another_user_private_state(
    auth_test_env: tuple[TestClient, str],
) -> None:
    """Acceptance criterion: User A cannot access User B's private state."""
    client, db_url = auth_test_env
    engine = get_engine(db_url)
    factory = get_session_factory(engine)

    with factory() as db:
        lib = Library(name="Lib", path="/books")
        db.add(lib)
        db.flush()

        book = Book(library_id=lib.id, title="1984")
        user_a = auth_service.create_user(db, "usera", "pass12345", "User A")
        user_b = auth_service.create_user(db, "userb", "pass12345", "User B")
        db.add(book)
        db.flush()

        # User B saves private reading progress
        prog_b = ReadingProgress(user_id=user_b.id, book_id=book.id, progression=0.88)
        db.add(prog_b)
        db.commit()

        user_a_id = user_a.id
        user_b_id = user_b.id

    # Verify via AuthorizationService that User A cannot access User B's data
    with factory() as db:
        current_a = db.get(User, user_a_id)
        assert current_a is not None
        assert authorization_service.can_access_user_data(current_a, user_b_id) is False

        with pytest.raises(PermissionError):
            authorization_service.require_user_access(current_a, user_b_id)


def test_disabled_user_cannot_authenticate(auth_test_env: tuple[TestClient, str]) -> None:
    """Acceptance criterion: Disabled users cannot authenticate or use active sessions."""
    client, db_url = auth_test_env
    engine = get_engine(db_url)
    factory = get_session_factory(engine)

    # Create user and log in while active
    with factory() as db:
        user = auth_service.create_user(db, "inactive_user", "password123", "Inactive")
        user_id = user.id

    login_resp = client.post(
        "/login", json={"username": "inactive_user", "password": "password123"}
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["token"]

    # Deactivate user
    with factory() as db:
        auth_service.disable_user(db, user_id)

    # Attempt to log in again: must be rejected with 401
    bad_login = client.post("/login", json={"username": "inactive_user", "password": "password123"})
    assert bad_login.status_code == 401

    # Attempt to use previous session token: must be rejected with 401 or 403
    session_req = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert session_req.status_code in (401, 403)


def test_admin_only_endpoints_reject_normal_users(
    auth_test_env: tuple[TestClient, str],
) -> None:
    """Acceptance criterion: Admin-only endpoints reject normal users with HTTP 403."""
    client, db_url = auth_test_env
    engine = get_engine(db_url)
    factory = get_session_factory(engine)

    with factory() as db:
        auth_service.create_user(db, "admin_user", "adminpass", "Admin", is_admin=True)
        auth_service.create_user(db, "normal_user", "userpass", "Normal", is_admin=False)

    # Log in as normal user
    resp_normal = client.post("/login", json={"username": "normal_user", "password": "userpass"})
    normal_token = resp_normal.json()["token"]

    # Log in as admin user
    resp_admin = client.post("/login", json={"username": "admin_user", "password": "adminpass"})
    admin_token = resp_admin.json()["token"]

    # Normal user calls admin endpoint -> 403 Forbidden
    resp_forbidden = client.get("/admin/users", headers={"Authorization": f"Bearer {normal_token}"})
    assert resp_forbidden.status_code == 403

    # Admin user calls admin endpoint -> 200 OK
    resp_allowed = client.get("/admin/users", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp_allowed.status_code == 200
    usernames = [u["username"] for u in resp_allowed.json()]
    assert "admin_user" in usernames
    assert "normal_user" in usernames


def test_change_password_and_logout(auth_test_env: tuple[TestClient, str]) -> None:
    """Verify password update and logout functionality."""
    client, db_url = auth_test_env
    engine = get_engine(db_url)
    factory = get_session_factory(engine)

    with factory() as db:
        auth_service.create_user(db, "password_changer", "oldpassword123", "Changer")

    # Log in
    login_resp = client.post(
        "/login", json={"username": "password_changer", "password": "oldpassword123"}
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["token"]

    # Change password
    change_resp = client.post(
        "/change-password",
        json={"old_password": "oldpassword123", "new_password": "brandnewpassword456"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert change_resp.status_code == 200

    # Old password no longer works
    fail_login = client.post(
        "/login", json={"username": "password_changer", "password": "oldpassword123"}
    )
    assert fail_login.status_code == 401

    # New password works
    success_login = client.post(
        "/login", json={"username": "password_changer", "password": "brandnewpassword456"}
    )
    assert success_login.status_code == 200

    # Test logout
    logout_resp = client.post("/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout_resp.status_code == 200


def test_cli_user_create_and_list(tmp_path: Path) -> None:
    """Verify 'bookserver user create' and 'bookserver user list' CLI commands."""
    runner = CliRunner()
    cfg_dir = tmp_path / "cli_auth_cfg"
    cfg_dir.mkdir()
    db_file = cfg_dir / "buku_cli.db"

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[paths]
config_dir = "{cfg_dir}"

[database]
url = "sqlite:///{db_file}"
"""
    )

    # Run migration first
    mig_res = runner.invoke(cli, ["migrate", "--config", str(config_file)])
    assert mig_res.exit_code == 0

    # Create admin user
    create_res = runner.invoke(
        cli,
        [
            "user",
            "create",
            "--username",
            "superadmin",
            "--password",
            "secretpass",
            "--admin",
            "--config",
            str(config_file),
        ],
    )
    assert create_res.exit_code == 0
    assert "superadmin" in create_res.output
    assert "admin=True" in create_res.output

    # List users
    list_res = runner.invoke(cli, ["user", "list", "--config", str(config_file)])
    assert list_res.exit_code == 0
    assert "superadmin" in list_res.output
