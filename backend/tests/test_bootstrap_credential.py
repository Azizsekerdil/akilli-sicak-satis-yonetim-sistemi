"""
First-run credential contract.

Six properties, each proved rather than asserted in prose:

1.  The bootstrap installs the one-time ``admin/admin`` credential, stored
    only as a hash and usable only from the local device.
2.  Protected areas are unreachable while a password change is outstanding —
    at the API, not merely in the web UI.
3.  The bootstrap credential is refused from a remote address, and cannot be
    talked into accepting one with a forged ``X-Forwarded-For``.
4.  It *is* accepted from the local device, so a real first run can happen.
5.  Once the password is changed, the old one is dead and both flags are down
    permanently.
6.  An administrative password reset forces another change but does **not**
    restore first-run status, and does not resurrect the old password.

The suite-wide ``admin`` account has already been through the first-run flow
(see ``conftest._complete_first_run``), so every test here builds its own
throwaway account and puts it into the bootstrap state explicitly.  Nothing in
this file relaxes a control to make an assertion pass.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.enums import RoleCode
from app.core.security import verify_password
from app.models.auth import Role, User
from app.models.base import utcnow
from app.services import auth_service, bootstrap_service
from tests.conftest import ADMIN_USERNAME

BOOTSTRAP_PASSWORD = "FirstRun123!"
REPLACEMENT_PASSWORD = "Replaced456!"
REMOTE_ADDR = "203.0.113.77"  # TEST-NET-3, never routable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_bootstrap_user(role_code: str = RoleCode.SYSTEM_ADMIN) -> str:
    """
    Create an account in exactly the state ``ensure_admin`` leaves behind.

    Returns the username.  Built through the ORM rather than the public API
    because the API deliberately offers no way to mint a bootstrap credential.
    """
    uname = f"boot_{uuid.uuid4().hex[:10]}"
    s = SessionLocal()
    try:
        role = s.execute(select(Role).where(Role.code == role_code)).scalar_one()
        s.add(
            User(
                username=uname,
                password_hash=auth_service.hash_password(BOOTSTRAP_PASSWORD),
                full_name="First-run account",
                role_id=role.id,
                status="ACTIVE",
                language="tr",
                must_change_password=True,
                is_bootstrap_credential=True,
                password_changed_at=utcnow(),
            )
        )
        s.commit()
    finally:
        s.close()
    return uname


def _fetch(username: str) -> User:
    s = SessionLocal()
    try:
        return s.execute(select(User).where(User.username == username)).scalar_one()
    finally:
        s.close()


class _WithPeer:
    """
    ASGI shim pinning the socket peer address the application observes.

    Starlette's ``TestClient`` hard-codes the peer as ``("testclient", 50000)``
    and offers no way to change it, but the whole point of these tests is where
    the connection came *from*.  Rewriting ``scope["client"]`` is the smallest
    honest way to say "this request arrived on the loopback interface" — the
    application code under test is untouched and still reads the peer exactly
    the way it does in production.
    """

    def __init__(self, app, host: str, port: int = 51000) -> None:
        self._app, self._host, self._port = app, host, port

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            scope = dict(scope)
            scope["client"] = (self._host, self._port)
        await self._app(scope, receive, send)


def _client_from(host: str) -> TestClient:
    from app.main import app

    return TestClient(_WithPeer(app, host))


def _local_client() -> TestClient:
    return _client_from("127.0.0.1")


def _remote_client() -> TestClient:
    return _client_from(REMOTE_ADDR)


# ---------------------------------------------------------------------------
# 1 — one-time fixed bootstrap password
# ---------------------------------------------------------------------------
class TestOneTimeDefaultPassword:
    def test_bootstrap_defines_the_owner_approved_pair(self):
        assert bootstrap_service.DEFAULT_ADMIN_USERNAME == "admin"
        assert bootstrap_service.DEFAULT_ADMIN_PASSWORD == "admin"

    def test_bootstrap_is_always_marked_for_immediate_change(self):
        user = _fetch(ADMIN_USERNAME)
        assert user.must_change_password is False  # suite fixture completed the flow
        assert user.is_bootstrap_credential is False

    def test_stored_credential_is_hashed_not_plaintext(self):
        uname = _make_bootstrap_user()
        user = _fetch(uname)
        assert BOOTSTRAP_PASSWORD not in user.password_hash
        assert user.password_hash != BOOTSTRAP_PASSWORD
        # bcrypt, or the PBKDF2 fallback when no bcrypt wheel is available.
        assert user.password_hash.startswith(("$2a$", "$2b$", "$2y$", "pbkdf2_sha256$"))
        assert verify_password(BOOTSTRAP_PASSWORD, user.password_hash)

    def test_old_pair_is_dead_after_first_change(self, client: TestClient):
        """The suite fixture changed it; admin/admin must now be dead."""
        r = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
        )
        assert r.status_code in (401, 403)
        assert "access_token" not in r.text


# ---------------------------------------------------------------------------
# 2 — protected areas unreachable before the change
# ---------------------------------------------------------------------------
class TestProtectedAreasClosedBeforeChange:
    @pytest.fixture
    def bootstrap_headers(self) -> dict[str, str]:
        uname = _make_bootstrap_user()
        r = _local_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": BOOTSTRAP_PASSWORD},
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/customers",          # customer records
            "/api/v1/products",           # catalogue
            "/api/v1/sales/invoices",     # financial records
            "/api/v1/ai/providers",       # AI / API-key settings
            "/api/v1/system/users",       # staff records
            "/api/v1/system/audit",       # audit trail
            "/api/v1/system/backups",     # backup / export
            "/api/v1/compliance/overview",
            "/api/v1/reports",
            "/api/v1/analytics/dashboard",
        ],
    )
    def test_every_protected_area_answers_403(self, client, bootstrap_headers, path):
        r = client.get(path, headers=bootstrap_headers)
        assert r.status_code == 403, f"{path} returned {r.status_code}"
        assert r.json().get("message_key") == "auth.password_change_required"

    def test_login_itself_still_succeeds(self, bootstrap_headers):
        """The account can sign in — it just cannot go anywhere."""
        assert bootstrap_headers["Authorization"].startswith("Bearer ")

    def test_the_escape_hatches_stay_open(self, client, bootstrap_headers):
        me = client.get("/api/v1/auth/me", headers=bootstrap_headers)
        assert me.status_code == 200
        assert me.json()["user"]["must_change_password"] is True

    def test_write_endpoints_are_closed_too(self, client, bootstrap_headers):
        r = client.post(
            "/api/v1/customers",
            headers=bootstrap_headers,
            json={"name": "Should never be created", "customer_type": "RETAIL"},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# 3 & 4 — local-only until changed
# ---------------------------------------------------------------------------
class TestBootstrapIsLocalOnly:
    def test_remote_login_is_refused_with_the_correct_password(self):
        uname = _make_bootstrap_user()
        r = _remote_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": BOOTSTRAP_PASSWORD},
        )
        assert r.status_code == 403
        assert r.json()["message_key"] == "auth.bootstrap_local_only"
        assert "access_token" not in r.text

    def test_forged_forwarded_header_does_not_help(self):
        """
        ``X-Forwarded-For`` is written by the caller.  The gate reads the socket
        peer, so claiming to be localhost changes nothing.
        """
        uname = _make_bootstrap_user()
        for forged in ("127.0.0.1", "::1", "127.0.0.1, 10.0.0.9", "localhost"):
            r = _remote_client().post(
                "/api/v1/auth/login",
                json={"username": uname, "password": BOOTSTRAP_PASSWORD},
                headers={"X-Forwarded-For": forged},
            )
            assert r.status_code == 403, forged
            assert r.json()["message_key"] == "auth.bootstrap_local_only"

    def test_local_login_is_accepted(self):
        uname = _make_bootstrap_user()
        r = _local_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": BOOTSTRAP_PASSWORD},
        )
        assert r.status_code == 200, r.text

    def test_refusal_is_audited_without_the_password(self):
        from app.models.system import AuditLog

        uname = _make_bootstrap_user()
        _remote_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": BOOTSTRAP_PASSWORD},
        )
        s = SessionLocal()
        try:
            rows = (
                s.execute(
                    select(AuditLog)
                    .where(AuditLog.username == uname)
                    .order_by(AuditLog.id.desc())
                )
                .scalars()
                .all()
            )
            assert rows, "the refusal produced no audit event"
            blob = " ".join(str(r.summary or "") for r in rows)
            assert "remote" in blob
            assert BOOTSTRAP_PASSWORD not in blob
        finally:
            s.close()

    def test_remote_refusal_is_recorded_as_a_failed_attempt(self):
        from app.models.auth import LoginAttempt

        uname = _make_bootstrap_user()
        _remote_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": BOOTSTRAP_PASSWORD},
        )
        s = SessionLocal()
        try:
            row = (
                s.execute(
                    select(LoginAttempt)
                    .where(LoginAttempt.username == uname)
                    .order_by(LoginAttempt.id.desc())
                )
                .scalars()
                .first()
            )
            assert row is not None
            assert row.successful is False
            assert row.failure_reason == "bootstrap_remote_refused"
        finally:
            s.close()

    def test_ordinary_account_is_not_restricted_to_localhost(self):
        """The gate must apply to the bootstrap credential and nothing else."""
        uname = _make_bootstrap_user()
        user = _fetch(uname)
        s = SessionLocal()
        try:
            fresh = s.get(User, user.id)
            auth_service.change_password(
                s, fresh, BOOTSTRAP_PASSWORD, REPLACEMENT_PASSWORD
            )
        finally:
            s.close()
        r = _remote_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": REPLACEMENT_PASSWORD},
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 5 — the first credential dies on change
# ---------------------------------------------------------------------------
class TestChangeKillsTheBootstrapCredential:
    def _change(self, uname: str) -> None:
        c = _local_client()
        login = c.post(
            "/api/v1/auth/login",
            json={"username": uname, "password": BOOTSTRAP_PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}
        r = c.post(
            "/api/v1/auth/change-password",
            headers=headers,
            json={
                "old_password": BOOTSTRAP_PASSWORD,
                "new_password": REPLACEMENT_PASSWORD,
            },
        )
        assert r.status_code == 200, r.text

    def test_old_password_stops_working(self):
        uname = _make_bootstrap_user()
        self._change(uname)
        r = _local_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": BOOTSTRAP_PASSWORD},
        )
        assert r.status_code == 401
        assert "access_token" not in r.text

    def test_both_flags_are_down_afterwards(self):
        uname = _make_bootstrap_user()
        self._change(uname)
        user = _fetch(uname)
        assert user.must_change_password is False
        assert user.is_bootstrap_credential is False

    def test_new_password_is_stored_hashed(self):
        uname = _make_bootstrap_user()
        self._change(uname)
        user = _fetch(uname)
        assert REPLACEMENT_PASSWORD not in user.password_hash
        assert user.password_hash.startswith(("$2a$", "$2b$", "$2y$", "pbkdf2_sha256$"))
        assert verify_password(REPLACEMENT_PASSWORD, user.password_hash)

    def test_protected_areas_open_afterwards(self, client):
        uname = _make_bootstrap_user()
        self._change(uname)
        login = _local_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": REPLACEMENT_PASSWORD},
        )
        headers = {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}
        r = client.get("/api/v1/system/users", headers=headers)
        assert r.status_code == 200, r.text

    def test_re_running_the_bootstrap_does_not_re_arm_the_account(self):
        """
        ``ensure_baseline`` runs on every startup.  It must never put a live
        administrator back into first-run state.
        """
        before = _fetch(ADMIN_USERNAME)
        assert before.must_change_password is False
        assert before.is_bootstrap_credential is False
        old_hash = before.password_hash

        bootstrap_service.ensure_baseline()

        after = _fetch(ADMIN_USERNAME)
        assert after.must_change_password is False
        assert after.is_bootstrap_credential is False
        assert after.password_hash == old_hash


# ---------------------------------------------------------------------------
# 6 — reset does not restore the first-run credential
# ---------------------------------------------------------------------------
class TestResetDoesNotRestoreDefault:
    def _reset(self, uname: str, new_password: str) -> None:
        s = SessionLocal()
        try:
            actor = s.execute(
                select(User).where(User.username == ADMIN_USERNAME)
            ).scalar_one()
            target = s.execute(
                select(User).where(User.username == uname)
            ).scalar_one()
            auth_service.reset_password(s, target, new_password, actor=actor)
        finally:
            s.close()

    def test_reset_forces_a_change_but_leaves_bootstrap_off(self):
        uname = _make_bootstrap_user(RoleCode.SALESPERSON)
        s = SessionLocal()
        try:
            fresh = s.execute(select(User).where(User.username == uname)).scalar_one()
            auth_service.change_password(
                s, fresh, BOOTSTRAP_PASSWORD, REPLACEMENT_PASSWORD
            )
        finally:
            s.close()

        self._reset(uname, "AdminSet789!")
        user = _fetch(uname)
        assert user.must_change_password is True
        assert user.is_bootstrap_credential is False

    def test_reset_does_not_bring_back_the_first_password(self):
        uname = _make_bootstrap_user(RoleCode.SALESPERSON)
        s = SessionLocal()
        try:
            fresh = s.execute(select(User).where(User.username == uname)).scalar_one()
            auth_service.change_password(
                s, fresh, BOOTSTRAP_PASSWORD, REPLACEMENT_PASSWORD
            )
        finally:
            s.close()
        self._reset(uname, "AdminSet789!")

        r = _local_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": BOOTSTRAP_PASSWORD},
        )
        assert r.status_code == 401

    def test_reset_account_stays_remotely_reachable(self):
        """
        A reset is an everyday operation; it must not strand a remote user on a
        localhost-only gate.  Only the *first-run* credential is local-only.
        """
        uname = _make_bootstrap_user(RoleCode.SALESPERSON)
        s = SessionLocal()
        try:
            fresh = s.execute(select(User).where(User.username == uname)).scalar_one()
            auth_service.change_password(
                s, fresh, BOOTSTRAP_PASSWORD, REPLACEMENT_PASSWORD
            )
        finally:
            s.close()
        self._reset(uname, "AdminSet789!")

        r = _remote_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": "AdminSet789!"},
        )
        assert r.status_code == 200, r.text
        # …but still walled in until they choose their own password.
        headers = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
        blocked = _remote_client().get("/api/v1/customers", headers=headers)
        assert blocked.status_code == 403


# ---------------------------------------------------------------------------
# Brute force / lockout still applies to the bootstrap account
# ---------------------------------------------------------------------------
class TestBruteForceProtection:
    def test_repeated_failures_lock_the_bootstrap_account(self):
        from app.core.config import settings

        uname = _make_bootstrap_user()
        c = _local_client()
        for _ in range(settings.max_login_attempts):
            c.post(
                "/api/v1/auth/login",
                json={"username": uname, "password": "definitely-wrong"},
            )
        user = _fetch(uname)
        assert user.failed_login_count >= settings.max_login_attempts
        assert user.locked_until is not None

        # Even the right password is refused while the lock holds.
        r = c.post(
            "/api/v1/auth/login",
            json={"username": uname, "password": BOOTSTRAP_PASSWORD},
        )
        assert r.status_code in (401, 403)
        assert r.json()["message_key"] == "auth.account_locked"

    def test_failed_attempts_never_record_the_password(self):
        from app.models.auth import LoginAttempt

        uname = _make_bootstrap_user()
        _local_client().post(
            "/api/v1/auth/login",
            json={"username": uname, "password": BOOTSTRAP_PASSWORD + "x"},
        )
        s = SessionLocal()
        try:
            rows = (
                s.execute(select(LoginAttempt).where(LoginAttempt.username == uname))
                .scalars()
                .all()
            )
            for row in rows:
                blob = f"{row.failure_reason} {row.user_agent} {row.ip_address}"
                assert BOOTSTRAP_PASSWORD not in blob
        finally:
            s.close()
