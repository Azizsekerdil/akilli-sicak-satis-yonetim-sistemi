"""
Pytest fixtures.

Every test runs against a **fresh temporary SQLite file** (not the developer's
database), with the schema created and reference data seeded.  A file rather
than ``:memory:`` because the backup/restore tests need a real file on disk and
WAL behaviour must match production.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

# --- Environment must be set BEFORE app.core.config is imported anywhere ----
_TMP_DIR = Path(tempfile.mkdtemp(prefix="vansales_test_"))
_DB_FILE = _TMP_DIR / "test.db"

os.environ["VS_ENV"] = "test"
os.environ["VS_DEBUG"] = "true"
os.environ["VS_DATABASE_URL"] = f"sqlite:///{_DB_FILE.as_posix()}"
os.environ["VS_SECRET_KEY"] = "test-secret-key-not-used-in-production-0123456789abcdef"
os.environ["VS_ADMIN_PASSWORD"] = "TestAdmin123!"
os.environ["VS_LOG_DIR"] = str(_TMP_DIR / "logs")
os.environ["VS_BACKUP_DIR"] = str(_TMP_DIR / "backups")
os.environ["VS_LOG_LEVEL"] = "WARNING"
os.environ["VS_RATE_LIMIT_PER_MINUTE"] = "100000"
os.environ["VS_LOGIN_RATE_LIMIT_PER_MINUTE"] = "100000"
# Keep tests hermetic: no outbound AI calls unless a test opts in.
os.environ["VS_LMSTUDIO_ENABLED"] = "false"
os.environ["VS_NVIDIA_ENABLED"] = "false"
os.environ["VS_CLAUDE_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import SessionLocal, create_all, drop_all  # noqa: E402
from app.services import bootstrap_service  # noqa: E402

class RouteView:
    """
    One mounted API operation: its **effective** path, its methods and the
    dependency graph FastAPI resolved for it.

    A thin view rather than the route object itself because the two things the
    tests need — the full path and the dependant — no longer live on the same
    object in every FastAPI version.
    """

    __slots__ = ("path", "methods", "dependant", "endpoint", "name")

    def __init__(self, path, methods, dependant, endpoint=None, name=None):
        self.path = path
        self.methods = set(methods or ())
        self.dependant = dependant
        self.endpoint = endpoint
        self.name = name

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<RouteView {sorted(self.methods)} {self.path}>"


def iter_api_routes(app=None) -> list["RouteView"]:
    """
    Every API operation the application actually serves, with its full path.

    ``app.routes`` is not a flat list of routes and has not been one since
    FastAPI 0.141: ``include_router`` keeps a nested ``_IncludedRouter`` rather
    than splicing the child's routes into the parent, so the familiar
    ``[r for r in app.routes if isinstance(r, APIRoute)]`` quietly returns the
    three top-level routes and misses the entire ``/api/v1`` surface.  A test
    built on that idiom does not fail on upgrade — it passes vacuously, which
    is worse.

    Both layouts are handled, and the result is asserted non-empty by every
    caller so an unknown third layout fails loudly instead of silently.
    """
    from fastapi.routing import APIRoute

    if app is None:
        from app.main import app as app_

        app = app_

    out: list[RouteView] = []
    seen: set[int] = set()

    def walk(node) -> None:
        if node is None or id(node) in seen:
            return
        seen.add(id(node))

        # FastAPI >= 0.141: an included router exposes its mounted operations
        # with the prefix already applied.
        contexts = getattr(node, "effective_route_contexts", None)
        if callable(contexts):
            for ctx in contexts():
                out.append(
                    RouteView(
                        ctx.path,
                        getattr(ctx, "methods", ()) or (),
                        getattr(ctx, "dependant", None),
                        getattr(getattr(ctx, "original_route", None), "endpoint", None),
                        getattr(ctx, "name", None),
                    )
                )
            return

        for route in getattr(node, "routes", []) or []:
            if isinstance(route, APIRoute):
                out.append(
                    RouteView(route.path, route.methods, route.dependant,
                              route.endpoint, route.name)
                )
            walk(route)
            walk(getattr(route, "app", None))

    walk(app)
    return out


ADMIN_USERNAME = "admin"

#: What the first-run bootstrap installs (``VS_ADMIN_PASSWORD``, set above).
#: The administrator is *locked down* while this password is in force: every
#: route outside the password-change flow answers 403, and sign-in is refused
#: from anything but the local device.
ADMIN_BOOTSTRAP_PASSWORD = "admin"

#: What the rest of the suite signs in with.  The session fixture completes the
#: first-run flow for real — it does not switch the gate off — so ordinary
#: tests exercise an ordinary, fully-provisioned administrator.
ADMIN_PASSWORD = "TestAdmin456!"


def _complete_first_run() -> None:
    """
    Walk the bootstrap administrator through the mandatory password change.

    Same call path the UI uses, so the flags clear exactly as they do in a real
    installation.  Tests that need to observe the *un*-changed bootstrap state
    build their own throwaway account rather than weakening this one — see
    ``tests/test_bootstrap_credential.py``.
    """
    from sqlalchemy import select

    from app.models.auth import User
    from app.services import auth_service

    s = SessionLocal()
    try:
        admin = s.execute(
            select(User).where(User.username == ADMIN_USERNAME)
        ).scalar_one()
        auth_service.change_password(
            s, admin, ADMIN_BOOTSTRAP_PASSWORD, ADMIN_PASSWORD
        )
        assert admin.must_change_password is False
        assert admin.is_bootstrap_credential is False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Session-scoped schema
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _schema() -> Generator[None, None, None]:
    drop_all()
    create_all()
    bootstrap_service.ensure_baseline()
    _complete_first_run()
    yield
    try:
        drop_all()
    except Exception:
        pass


@pytest.fixture
def db() -> Generator[Session, None, None]:
    """A plain session.  Tests that write should clean up after themselves."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def client() -> Generator[TestClient, None, None]:
    from app.main import app

    # The context manager triggers the lifespan (schema + seed).
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin_token(client: TestClient) -> str:
    r = client.post(
        "/api/v1/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return r.json()["tokens"]["access_token"]


@pytest.fixture(scope="session")
def auth(admin_token: str) -> dict[str, str]:
    """Authorization header for the system administrator."""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def make_user(client: TestClient, auth: dict[str, str]):
    """
    Factory creating a user with a given role and returning its auth header.

    Used by the permission tests to prove that a salesperson really cannot
    reach an administrator's endpoints.
    """
    created: list[str] = []

    def _make(role_code: str, *, username: str | None = None) -> dict[str, str]:
        uname = username or f"t_{role_code.lower()}_{len(created)}"
        password = "TestUser123!"
        from app.core.db import SessionLocal as SL
        from app.services import auth_service

        s = SL()
        try:
            actor = None
            from sqlalchemy import select

            from app.models.auth import User

            actor = s.execute(
                select(User).where(User.username == ADMIN_USERNAME)
            ).scalar_one()
            auth_service.create_user(
                s,
                username=uname,
                password=password,
                full_name=f"Test {role_code}",
                role_code=role_code,
                actor=actor,
            )
        finally:
            s.close()

        r = client.post(
            "/api/v1/auth/login", json={"username": uname, "password": password}
        )
        assert r.status_code == 200, r.text
        created.append(uname)
        return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}

    return _make


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def api() -> str:
    return "/api/v1"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Rate limiting must not make one test fail because another ran first."""
    from app.core.middleware import reset_rate_limits

    reset_rate_limits()
    yield
