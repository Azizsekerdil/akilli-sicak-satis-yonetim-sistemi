"""
Every URL the web client calls must exist on the server.

This is not a theoretical concern.  Before this test existed, five of the six
compliance screens talked to endpoints that were never implemented — the list
called ``/compliance/inventory`` while the API served
``/compliance/inventory/fields``, the request screen called
``/compliance/data-subject-requests`` against ``/compliance/dsr``, and the
appeal button posted into thin air.  Every one of those screens rendered, and
every one of them 404'd the moment a person used it.  A screen that cannot
reach its data is decoration.

The check reads the *live* route table and the frontend source, so it costs
nothing to keep true and cannot be satisfied by a comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"

#: `api.get<Foo<Bar>>('/path', …)` / `api.post(`/path/${id}`)` — the generic
#: parameter may itself contain angle brackets, hence the non-greedy `.*?`.
CALL = re.compile(
    r"api\.(get|post|put|patch|delete)\s*(?:<.*?>)?\s*\(\s*[`'\"]([^`'\"]+)[`'\"]",
    re.S,
)

#: `${...}` in a template literal is a value substituted at runtime.  It may be
#: a path parameter (an id) or, in a couple of places, the last path segment
#: itself (`/campaigns/${id}/${action}` where action is 'activate' | 'pause').
#: Both are matched against the route table as wildcards.
PLACEHOLDER = "\x00"


def _pattern(path: str) -> list[str]:
    """
    Split a frontend path into segments, with runtime values as wildcards.

    Substitution happens *before* the query string is dropped: an expression
    like ``${consent?.id}`` contains a question mark, and splitting on "?"
    first would truncate the path mid-placeholder and silently turn a working
    call into a false failure.
    """
    cleaned = re.sub(r"\$\{[^}]*\}", PLACEHOLDER, path)
    return cleaned.split("?")[0].rstrip("/").split("/")


def _route_pattern(path: str) -> list[str]:
    return re.sub(r"\{[^}]+\}", PLACEHOLDER, path).rstrip("/").split("/")


def _matches(call: list[str], route: list[str]) -> bool:
    if len(call) != len(route):
        return False
    for c, r in zip(call, route):
        if r == PLACEHOLDER or c == PLACEHOLDER:
            continue
        if c != r:
            return False
    return True


@pytest.fixture(scope="module")
def server_routes() -> list[tuple[list[str], set[str]]]:
    from tests.conftest import iter_api_routes

    out: list[tuple[list[str], set[str]]] = []
    for r in iter_api_routes():
        if r.path.startswith("/api/v1"):
            out.append(
                (_route_pattern(r.path[len("/api/v1") :]), r.methods - {"HEAD", "OPTIONS"})
            )
    assert out, "no API routes discovered"
    return out


@pytest.fixture(scope="module")
def client_calls() -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    for source in sorted(FRONTEND.rglob("*.ts")) + sorted(FRONTEND.rglob("*.tsx")):
        text = source.read_text(encoding="utf-8")
        for m in CALL.finditer(text):
            path = m.group(2)
            if not path.startswith("/"):
                continue  # a relative or computed path; nothing to resolve here
            line = text[: m.start()].count("\n") + 1
            calls.append(
                (m.group(1).upper(), path, f"{source.relative_to(FRONTEND)}:{line}")
            )
    assert calls, "no API calls found in the frontend — has the client moved?"
    return calls


class TestFrontendCallsResolve:
    def test_every_call_hits_a_real_route(self, server_routes, client_calls):
        broken = []
        for method, path, where in client_calls:
            pattern = _pattern(path)
            if not any(_matches(pattern, route) for route, _ in server_routes):
                broken.append(f"{method} {path}  ({where})")
        assert broken == [], "frontend calls with no matching route:\n  " + "\n  ".join(broken)

    def test_every_call_uses_a_method_the_route_accepts(self, server_routes, client_calls):
        wrong = []
        for method, path, where in client_calls:
            pattern = _pattern(path)
            allowed: set[str] = set()
            for route, methods in server_routes:
                if _matches(pattern, route):
                    allowed |= methods
            if allowed and method not in allowed:
                wrong.append(f"{method} {path} (route accepts {sorted(allowed)}) ({where})")
        assert wrong == [], "frontend calls using an unsupported method:\n  " + "\n  ".join(wrong)

    def test_compliance_screens_are_covered(self, client_calls):
        """Guard against the check passing because the screens were deleted."""
        compliance = [c for c in client_calls if c[1].startswith("/compliance")]
        assert len(compliance) >= 12, (
            f"only {len(compliance)} compliance API calls found — screens missing?"
        )


class TestPermissionKeysAreReal:
    """
    ``can('compliance.rule_packs', 'APPROVE')`` is always false: the catalogue
    spells it ``compliance.rulepacks``.  A permission check against a name that
    does not exist silently hides the control it guards, which looks exactly
    like "the user lacks permission" and is impossible to diagnose from the UI.
    """

    @pytest.fixture(scope="class")
    def ui_permission_refs(self) -> list[tuple[str, str, str]]:
        pattern = re.compile(r"can\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")
        refs = []
        for source in sorted(FRONTEND.rglob("*.tsx")):
            text = source.read_text(encoding="utf-8")
            for m in pattern.finditer(text):
                line = text[: m.start()].count("\n") + 1
                refs.append((m.group(1), m.group(2), f"{source.relative_to(FRONTEND)}:{line}"))
        assert refs, "no permission checks found in the UI"
        return refs

    def test_every_resource_name_exists(self, ui_permission_refs):
        from app.core.permissions import RESOURCE_BY_KEY

        unknown = [
            f"{resource}:{action} ({where})"
            for resource, action, where in ui_permission_refs
            if resource not in RESOURCE_BY_KEY
        ]
        assert unknown == [], "UI checks permissions that do not exist:\n  " + "\n  ".join(unknown)

    def test_every_action_is_supported_by_its_resource(self, ui_permission_refs):
        from app.core.permissions import RESOURCE_BY_KEY

        unsupported = []
        for resource, action, where in ui_permission_refs:
            res = RESOURCE_BY_KEY.get(resource)
            if res is not None and action not in res.actions:
                unsupported.append(
                    f"{resource}:{action} (resource supports {sorted(res.actions)}) ({where})"
                )
        assert unsupported == [], (
            "UI checks actions a resource does not define:\n  " + "\n  ".join(unsupported)
        )

    def test_route_guards_name_real_resources(self):
        from app.core.permissions import RESOURCE_BY_KEY

        unknown = []
        for name in ("App.tsx", "components/Layout.tsx"):
            text = (FRONTEND / name).read_text(encoding="utf-8")
            for m in re.finditer(r"resource:\s*'([^']+)'", text):
                if m.group(1) not in RESOURCE_BY_KEY:
                    unknown.append(f"{name}: {m.group(1)}")
        assert unknown == [], "route/menu guards name unknown resources:\n  " + "\n  ".join(unknown)
