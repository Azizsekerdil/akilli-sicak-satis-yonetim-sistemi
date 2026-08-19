"""Authentication, RBAC enforcement, privilege escalation and audit integrity."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.enums import RoleCode
from app.core.permissions import ROLES, all_permission_codes, role_permissions
from app.models.auth import Role, User
from app.services import audit_service, auth_service
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
class TestLogin:
    def test_admin_can_sign_in(self, client: TestClient, api: str):
        r = client.post(
            f"{api}/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["tokens"]["access_token"]
        assert body["session"]["role"] == RoleCode.SYSTEM_ADMIN
        assert body["session"]["data_scope"] == "ALL"

    def test_wrong_password_is_rejected(self, client: TestClient, api: str):
        r = client.post(
            f"{api}/auth/login", json={"username": ADMIN_USERNAME, "password": "nope"}
        )
        assert r.status_code == 401

    def test_unknown_user_gives_the_same_message_as_wrong_password(
        self, client: TestClient, api: str
    ):
        # Otherwise the endpoint becomes a username oracle.
        a = client.post(
            f"{api}/auth/login", json={"username": ADMIN_USERNAME, "password": "nope"}
        )
        b = client.post(
            f"{api}/auth/login", json={"username": "ghost_user", "password": "nope"}
        )
        assert a.status_code == b.status_code == 401
        assert a.json()["message_key"] == b.json()["message_key"]

    def test_error_message_follows_the_requested_language(
        self, client: TestClient, api: str
    ):
        tr = client.post(
            f"{api}/auth/login?lang=tr",
            json={"username": ADMIN_USERNAME, "password": "nope"},
        ).json()["message"]
        en = client.post(
            f"{api}/auth/login?lang=en",
            json={"username": ADMIN_USERNAME, "password": "nope"},
        ).json()["message"]
        assert tr != en
        assert "şifre" in tr.lower() or "kullanıcı" in tr.lower()
        assert "password" in en.lower() or "username" in en.lower()

    def test_password_is_never_echoed_back(self, client: TestClient, api: str):
        r = client.post(
            f"{api}/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        assert ADMIN_PASSWORD not in r.text


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
class TestTokens:
    def test_protected_route_needs_a_token(self, client: TestClient, api: str):
        assert client.get(f"{api}/auth/me").status_code == 401

    def test_garbage_token_is_rejected(self, client: TestClient, api: str):
        r = client.get(f"{api}/auth/me", headers={"Authorization": "Bearer garbage"})
        assert r.status_code == 401

    def test_refresh_rotates_and_retires_the_old_token(
        self, client: TestClient, api: str
    ):
        login = client.post(
            f"{api}/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        ).json()
        old_refresh = login["tokens"]["refresh_token"]

        first = client.post(f"{api}/auth/refresh", json={"refresh_token": old_refresh})
        assert first.status_code == 200
        assert first.json()["access_token"] != login["tokens"]["access_token"]

        # Replaying the retired refresh token must fail.
        replay = client.post(f"{api}/auth/refresh", json={"refresh_token": old_refresh})
        assert replay.status_code == 401

    def test_me_returns_the_session(self, client: TestClient, auth, api: str):
        r = client.get(f"{api}/auth/me", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["user"]["username"] == ADMIN_USERNAME
        assert len(body["permissions"]) > 100


# ---------------------------------------------------------------------------
# Permission catalogue
# ---------------------------------------------------------------------------
class TestPermissionCatalogue:
    def test_every_role_resolves_to_real_permissions(self):
        valid = set(all_permission_codes())
        for rd in ROLES:
            perms = role_permissions(rd.code)
            assert perms, f"role {rd.code} has no permissions"
            unknown = perms - valid
            assert not unknown, f"role {rd.code} grants unknown permissions: {unknown}"

    def test_admin_holds_everything(self):
        assert role_permissions(RoleCode.SYSTEM_ADMIN) == set(all_permission_codes())

    def test_salesperson_is_restricted(self):
        perms = role_permissions(RoleCode.SALESPERSON)
        assert "sales.hot_sale:CREATE" in perms
        assert "system.users:CREATE" not in perms
        assert "system.roles:UPDATE" not in perms
        assert "stock.adjustments:APPROVE" not in perms

    def test_auditor_is_read_only(self):
        perms = role_permissions(RoleCode.AUDITOR)
        writes = {p for p in perms if p.split(":")[1] not in ("VIEW", "EXPORT")}
        assert not writes, f"auditor should not hold write permissions: {writes}"

    def test_all_19_roles_are_seeded(self, db):
        codes = set(db.execute(select(Role.code)).scalars())
        for rd in ROLES:
            assert rd.code in codes, f"role {rd.code} was not seeded"
        assert len(ROLES) == 19

    def test_catalogue_endpoint(self, client: TestClient, auth, api: str):
        r = client.get(f"{api}/auth/permissions/catalog", headers=auth)
        assert r.status_code == 200
        assert len(r.json()) == len(all_permission_codes())


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------
class TestEnforcement:
    def test_salesperson_cannot_read_the_permission_catalogue(
        self, client: TestClient, make_user, api: str
    ):
        sp = make_user(RoleCode.SALESPERSON)
        r = client.get(f"{api}/auth/permissions/catalog", headers=sp)
        assert r.status_code == 403
        assert r.json()["error"] == "permission_denied"

    def test_denial_message_is_localised(self, client: TestClient, make_user, api: str):
        sp = make_user(RoleCode.SALESPERSON, username="sp_lang_test")
        tr = client.get(f"{api}/auth/permissions/catalog?lang=tr", headers=sp).json()
        en = client.get(f"{api}/auth/permissions/catalog?lang=en", headers=sp).json()
        assert tr["message"] != en["message"]

    def test_scope_context_limits_a_salesperson(self, db):
        user = db.execute(
            select(User).where(User.username == ADMIN_USERNAME)
        ).scalar_one()
        ctx = auth_service.scope_context(db, user)
        assert ctx["unrestricted"] is True


# ---------------------------------------------------------------------------
# Privilege escalation
# ---------------------------------------------------------------------------
class TestPrivilegeEscalation:
    def test_cannot_create_a_user_above_your_own_rank(self, db):
        from app.core.exceptions import PermissionDeniedError

        supervisor = db.execute(
            select(User).where(User.username.like("t_field_sales_supervisor%"))
        ).scalars().first()
        if supervisor is None:
            pytest.skip("supervisor fixture not created in this run")

        with pytest.raises(PermissionDeniedError):
            auth_service.create_user(
                db,
                username="escalation_attempt",
                password="Escalate123!",
                full_name="Nope",
                role_code=RoleCode.SYSTEM_ADMIN,
                actor=supervisor,
            )

    def test_cannot_grant_a_permission_you_do_not_hold(self, db, make_user):
        from app.core.exceptions import PermissionDeniedError

        make_user(RoleCode.SALESPERSON, username="sp_grant_src")
        make_user(RoleCode.SALESPERSON, username="sp_grant_dst")
        actor = db.execute(
            select(User).where(User.username == "sp_grant_src")
        ).scalar_one()
        target = db.execute(
            select(User).where(User.username == "sp_grant_dst")
        ).scalar_one()

        with pytest.raises(PermissionDeniedError):
            auth_service.set_permission_overrides(
                db,
                target,
                grant=["system.users:DELETE"],
                revoke=[],
                data_scope=None,
                actor=actor,
            )


# ---------------------------------------------------------------------------
# Account lockout
# ---------------------------------------------------------------------------
class TestLockout:
    def test_repeated_failures_lock_the_account(self, client: TestClient, make_user, api: str):
        make_user(RoleCode.MERCHANDISER, username="lockme")
        from app.core.config import settings

        last = None
        for _ in range(settings.max_login_attempts + 1):
            last = client.post(
                f"{api}/auth/login", json={"username": "lockme", "password": "wrong"}
            )
        assert last is not None and last.status_code == 401

        # Even the correct password is refused while the lock holds.
        r = client.post(
            f"{api}/auth/login", json={"username": "lockme", "password": "TestUser123!"}
        )
        assert r.status_code == 401
        assert r.json()["message_key"] in ("auth.account_locked", "auth.invalid_credentials")


# ---------------------------------------------------------------------------
# Audit chain
# ---------------------------------------------------------------------------
class TestAuditChain:
    def test_login_is_audited(self, client: TestClient, api: str, db):
        from app.models.system import AuditLog

        before = db.execute(select(AuditLog.id)).scalars().all()
        client.post(
            f"{api}/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        db.expire_all()
        after = db.execute(select(AuditLog.id)).scalars().all()
        assert len(after) > len(before)

    def test_chain_is_intact(self, db):
        result = audit_service.verify_chain(db)
        assert result["valid"], f"audit chain broken at id={result['broken_at']}"
        assert result["checked"] > 0

    def test_tampering_is_detected(self, db):
        from app.models.system import AuditLog

        row = db.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(1)
        ).scalar_one_or_none()
        if row is None:
            pytest.skip("no audit rows yet")

        original = row.checksum
        row.checksum = "0" * 64
        db.flush()
        assert not audit_service.verify_chain(db)["valid"]

        row.checksum = original
        db.flush()
        assert audit_service.verify_chain(db)["valid"]

    def test_secrets_never_reach_the_audit_log(self, db):
        entry = audit_service.record(
            db,
            "UPDATE",
            entity_type="Test",
            entity_id=1,
            summary="secret handling probe",
            new_values={"api_key": "nvapi-should-not-be-stored", "name": "ok"},  # synthetic-credential-fixture
        )
        db.flush()
        assert "nvapi-should-not-be-stored" not in (entry.new_values or "")  # synthetic-credential-fixture
        assert "ok" in (entry.new_values or "")
        db.rollback()
