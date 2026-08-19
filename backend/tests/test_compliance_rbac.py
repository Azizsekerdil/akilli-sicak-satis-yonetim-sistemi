"""
The compliance permission tree must be real, not decorative.

Three failure modes are checked, because all three have been observed in this
codebase or are easy to reintroduce:

*   **Decorative permission** — the catalogue declares ``compliance.dsr:VIEW``,
    a role is granted it, and the endpoint checks something else entirely.  The
    role sees the menu entry and gets 403.  That is what happened when every
    compliance route hung off ``system.settings``.
*   **Over-broad gate** — one shared permission unlocks the whole module, so
    editing an AI setting also grants rule-pack approval.
*   **Missing gate** — a route with no permission dependency at all.

The first test walks the *live* route table and reads each route's actual
dependency, so it cannot drift away from the code the way a hand-maintained
list would.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.permissions import RESOURCE_BY_KEY, role_permissions
from app.core.enums import RoleCode

COMPLIANCE_PREFIX = "/api/v1/compliance"


# ---------------------------------------------------------------------------
# Reading the real gates off the live route table
# ---------------------------------------------------------------------------
def _required_permissions(route) -> set[tuple[str, str]]:
    """
    Every ``(resource, action)`` the route's dependency graph enforces.

    ``app.core.deps.require`` builds a closure over ``resource`` and ``act``;
    walking the resolved dependant and reading those free variables tells us
    what the route *actually* checks, rather than what a comment claims.
    """
    found: set[tuple[str, str]] = set()

    def walk(dep: Any) -> None:
        call = getattr(dep, "call", None)
        closure = getattr(call, "__closure__", None)
        code = getattr(call, "__code__", None)
        if closure and code and {"resource", "act"} <= set(code.co_freevars):
            values = dict(zip(code.co_freevars, (c.cell_contents for c in closure)))
            found.add((values["resource"], values["act"]))
        for sub in getattr(dep, "dependencies", []):
            walk(sub)

    walk(route.dependant)
    return found


@pytest.fixture(scope="module")
def compliance_routes() -> list:
    from tests.conftest import iter_api_routes

    routes = [r for r in iter_api_routes() if r.path.startswith(COMPLIANCE_PREFIX)]
    assert routes, "no compliance routes found — has the router been unmounted?"
    return routes


class TestEveryComplianceRouteIsGated:
    def test_every_route_enforces_at_least_one_permission(self, compliance_routes):
        ungated = [r.path for r in compliance_routes if not _required_permissions(r)]
        assert ungated == [], f"compliance routes with no permission gate: {ungated}"

    def test_every_gate_uses_a_declared_compliance_resource(self, compliance_routes):
        wrong: list[str] = []
        for route in compliance_routes:
            for resource, action in _required_permissions(route):
                if not resource.startswith(("compliance.", "hsp.")):
                    wrong.append(f"{route.path} -> {resource}:{action}")
        assert wrong == [], (
            "compliance routes gated on a non-compliance resource — the "
            f"compliance permission tree would be decorative: {wrong}"
        )

    def test_no_route_still_hangs_off_system_settings(self, compliance_routes):
        for route in compliance_routes:
            resources = {r for r, _ in _required_permissions(route)}
            assert "system.settings" not in resources, route.path

    def test_every_gate_names_a_real_permission(self, compliance_routes):
        unknown: list[str] = []
        for route in compliance_routes:
            for resource, action in _required_permissions(route):
                res = RESOURCE_BY_KEY.get(resource)
                if res is None or action not in res.actions:
                    unknown.append(f"{route.path} -> {resource}:{action}")
        assert unknown == [], f"gate names a permission that does not exist: {unknown}"

    def test_write_routes_do_not_settle_for_view(self, compliance_routes):
        """A POST/PUT/DELETE gated only on VIEW is a missing gate wearing a hat."""
        offenders: list[str] = []
        for route in compliance_routes:
            methods = route.methods - {"HEAD", "OPTIONS"}
            if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            actions = {a for _, a in _required_permissions(route)}
            if actions and actions <= {"VIEW"}:
                offenders.append(f"{sorted(methods)} {route.path}")
        assert offenders == [], f"write routes gated on VIEW only: {offenders}"

    def test_approval_is_separated_from_update(self, compliance_routes):
        """
        Putting a rule pack into force and closing a data-subject request are
        approvals.  They must not be reachable with a plain UPDATE.
        """
        wanted = {
            f"{COMPLIANCE_PREFIX}/rulepacks/{{pack_id}}/approve": ("compliance.rulepacks", "APPROVE"),
            f"{COMPLIANCE_PREFIX}/dsr/{{request_id}}/fulfil": ("compliance.dsr", "APPROVE"),
        }
        by_path = {r.path: r for r in compliance_routes}
        for path, expected in wanted.items():
            assert path in by_path, f"{path} is missing from the route table"
            assert expected in _required_permissions(by_path[path]), (
                f"{path} does not require {expected[0]}:{expected[1]}"
            )


# ---------------------------------------------------------------------------
# The catalogue and the role matrix agree
# ---------------------------------------------------------------------------
class TestRoleMatrixMatchesTheGates:
    def test_auditor_holds_read_access_to_every_compliance_read_gate(self, compliance_routes):
        """
        The auditor is the role the compliance module exists for.  Every read
        gate it meets must be a permission it actually holds — this is the
        assertion that used to be false.
        """
        held = role_permissions(RoleCode.AUDITOR)
        missing: list[str] = []
        for route in compliance_routes:
            if "GET" not in route.methods:
                continue
            for resource, action in _required_permissions(route):
                if action != "VIEW":
                    continue
                if f"{resource}:{action}" not in held:
                    missing.append(f"{route.path} needs {resource}:{action}")
        assert missing == [], f"AUDITOR cannot read what it is granted: {missing}"

    def test_auditor_holds_no_compliance_write_permission(self):
        held = role_permissions(RoleCode.AUDITOR)
        writes = {
            p
            for p in held
            if p.startswith(("compliance.", "hsp."))
            and p.split(":")[1] in {"CREATE", "UPDATE", "DELETE", "APPROVE", "EXECUTE"}
        }
        assert writes == set(), f"read-only auditor holds write permissions: {writes}"

    def test_salesperson_holds_no_compliance_permission_at_all(self):
        held = role_permissions(RoleCode.SALESPERSON)
        leaked = {p for p in held if p.startswith(("compliance.", "hsp."))}
        assert leaked == set(), f"salesperson holds compliance permissions: {leaked}"

    def test_administrator_can_still_operate_the_module(self):
        held = role_permissions(RoleCode.SYSTEM_ADMIN)
        for key, res in RESOURCE_BY_KEY.items():
            if not key.startswith(("compliance.", "hsp.")):
                continue
            for action in res.actions:
                assert f"{key}:{action}" in held, f"admin lost {key}:{action}"


# ---------------------------------------------------------------------------
# End to end, against the running API
# ---------------------------------------------------------------------------
READ_ENDPOINTS = [
    "/compliance/overview",
    "/compliance/inventory/fields",
    "/compliance/processing-activities",
    "/compliance/notices",
    "/compliance/consents",
    "/compliance/dsr",
    "/compliance/transfers",
    "/compliance/rulepacks",
    "/compliance/evidence",
    "/compliance/hsp/receipts",
]

WRITE_ENDPOINTS = [
    ("post", "/compliance/inventory/scan", {}),
    ("post", "/compliance/processing-activities", {"name": "x", "purpose_codes": []}),
    ("post", "/compliance/notices", {"title": "x", "body": "y"}),
    ("post", "/compliance/consents", {"subject_ref": "x", "purpose_code": "y"}),
    ("post", "/compliance/dsr", {"subject_ref": "x", "request_type": "ACCESS"}),
    ("post", "/compliance/transfers", {"recipient": "x", "country": "DE"}),
    ("post", "/compliance/hsp/evaluate", {"action_code": "x"}),
]


# ---------------------------------------------------------------------------
# Test accounts
# ---------------------------------------------------------------------------
_ACCOUNT_PASSWORD = "RbacProbe123!"


def _account(client, auth, role_code: str, username: str) -> dict[str, str]:
    """
    Create (once) and sign in a probe account for *role_code*.

    Written here rather than reusing the function-scoped ``make_user`` factory
    because these fixtures are class-scoped: the account must survive across the
    parametrised cases instead of being rebuilt for each one.
    """
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models.auth import User
    from app.services import auth_service
    from tests.conftest import ADMIN_USERNAME

    s = SessionLocal()
    try:
        existing = s.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        if existing is None:
            actor = s.execute(
                select(User).where(User.username == ADMIN_USERNAME)
            ).scalar_one()
            auth_service.create_user(
                s,
                username=username,
                password=_ACCOUNT_PASSWORD,
                full_name=f"RBAC probe {role_code}",
                role_code=role_code,
                actor=actor,
            )
    finally:
        s.close()

    r = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": _ACCOUNT_PASSWORD},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


class TestApiRefusesTheUnauthorised:
    @pytest.fixture(scope="class")
    def salesperson(self, client, auth) -> dict[str, str]:
        return _account(client, auth, RoleCode.SALESPERSON, "rbac_sales")

    @pytest.fixture(scope="class")
    def auditor(self, client, auth) -> dict[str, str]:
        return _account(client, auth, RoleCode.AUDITOR, "rbac_auditor")

    @pytest.mark.parametrize("path", READ_ENDPOINTS)
    def test_salesperson_is_refused_on_reads(self, client, api, salesperson, path):
        r = client.get(f"{api}{path}", headers=salesperson)
        assert r.status_code == 403, f"{path} -> {r.status_code}"

    @pytest.mark.parametrize("method,path,body", WRITE_ENDPOINTS)
    def test_salesperson_is_refused_on_writes(self, client, api, salesperson, method, path, body):
        r = getattr(client, method)(f"{api}{path}", headers=salesperson, json=body)
        assert r.status_code == 403, f"{path} -> {r.status_code}"

    @pytest.mark.parametrize("path", READ_ENDPOINTS)
    def test_auditor_is_allowed_to_read(self, client, api, auditor, path):
        r = client.get(f"{api}{path}", headers=auditor)
        assert r.status_code != 403, f"{path} refused the auditor: {r.text[:200]}"
        assert r.status_code < 500, f"{path} -> {r.status_code}"

    @pytest.mark.parametrize("method,path,body", WRITE_ENDPOINTS)
    def test_auditor_is_refused_on_writes(self, client, api, auditor, method, path, body):
        r = getattr(client, method)(f"{api}{path}", headers=auditor, json=body)
        assert r.status_code == 403, f"{path} -> {r.status_code}"

    @pytest.mark.parametrize("path", READ_ENDPOINTS)
    def test_anonymous_is_refused(self, client, api, path):
        r = client.get(f"{api}{path}")
        assert r.status_code == 401, f"{path} -> {r.status_code}"

    def test_administrator_can_read(self, client, api, auth):
        for path in READ_ENDPOINTS:
            r = client.get(f"{api}{path}", headers=auth)
            assert r.status_code != 403, f"{path} refused the administrator"


# ---------------------------------------------------------------------------
# The web UI points at the same permissions the API enforces
# ---------------------------------------------------------------------------
class TestUiLinksMatchTheApi:
    """
    A menu entry the API will refuse is a broken promise; a screen the menu
    hides but the API serves is a hole.  Both are checked by reading the
    frontend source — no browser needed.
    """

    @pytest.fixture(scope="class")
    def frontend_sources(self) -> dict[str, str]:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "frontend" / "src"
        return {
            "layout": (root / "components" / "Layout.tsx").read_text(encoding="utf-8"),
            "app": (root / "App.tsx").read_text(encoding="utf-8"),
        }

    def _resources(self, text: str, prefix: str) -> set[str]:
        import re

        return {
            m
            for m in re.findall(r"resource:\s*'([^']+)'", text)
            if m.startswith(prefix)
        }

    def test_menu_resources_exist_in_the_catalogue(self, frontend_sources):
        for name, text in frontend_sources.items():
            for resource in self._resources(text, "compliance.") | self._resources(text, "hsp."):
                assert resource in RESOURCE_BY_KEY, f"{name}: unknown resource {resource}"

    def test_menu_resources_are_actually_enforced_by_some_route(
        self, frontend_sources, compliance_routes
    ):
        enforced = {
            resource
            for route in compliance_routes
            for resource, _ in _required_permissions(route)
        }
        for name, text in frontend_sources.items():
            declared = self._resources(text, "compliance.") | self._resources(text, "hsp.")
            unenforced = declared - enforced
            assert unenforced == set(), (
                f"{name} links to screens whose resource no API route enforces: "
                f"{sorted(unenforced)}"
            )

    def test_every_compliance_screen_has_a_route_guard(self, frontend_sources):
        import re

        app_tsx = frontend_sources["app"]
        paths = re.findall(r"\{\s*path:\s*'(/compliance[^']*)',[^}]*\}", app_tsx)
        assert paths, "no compliance screens declared in App.tsx"
        for path in paths:
            block = re.search(
                r"\{\s*path:\s*'" + re.escape(path) + r"',[^}]*\}", app_tsx
            )
            assert block and "resource:" in block.group(0), (
                f"{path} is routed without a resource guard"
            )
