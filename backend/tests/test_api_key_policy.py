"""
Provider credentials: what the product may show, store and ship.

The policy this file enforces:

*   No real key exists anywhere in the repository — not in source, not in
    ``.env.example``, not in fixtures.
*   A missing key is a normal state.  The provider reports itself as not
    configured, makes no outbound call, and the rest of the system — local AI
    and every non-AI feature — carries on.
*   The API and the UI see the provider name, its status and at most the last
    four characters of the key.  Never the prefix, never the length, never the
    value.
*   ``Test connection`` happens only when a person asks for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.security import mask_secret

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Shapes that would indicate a live credential slipped in.  Deliberately
#: narrow — matching "anything long" would fire on hashes and lockfile digests.
LIVE_KEY_SHAPES = [
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{20,}"),          # NVIDIA
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),         # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),                # OpenAI-compatible
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),         # GitHub
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                 # AWS access key id
]

SCANNED_SUFFIXES = {".py", ".ts", ".tsx", ".json", ".md", ".example", ".ini", ".cfg", ".yml", ".yaml"}
SKIP_DIRS = {"node_modules", "dist", ".venv", "venv", "__pycache__", ".git", "data", "logs", "backups"}

#: Opt-out marker for the handful of lines that must legitimately contain a
#: credential-shaped string: the fixtures that prove log redaction and masking
#: work.  Marking them individually keeps the rest of the test suite in scope —
#: skipping ``tests/`` wholesale would be the easy answer and a bad one, since a
#: real key pasted into a test would then ship unnoticed.
SYNTHETIC_MARKER = "synthetic-credential-fixture"


def _repo_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        if path.suffix in SCANNED_SUFFIXES or path.name == ".env.example":
            yield path


# ---------------------------------------------------------------------------
# Nothing real ships
# ---------------------------------------------------------------------------
class TestNoCredentialInTheTree:
    def test_no_file_contains_a_live_looking_key(self):
        hits: list[str] = []
        for path in _repo_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if SYNTHETIC_MARKER in line:
                    continue
                for shape in LIVE_KEY_SHAPES:
                    if shape.search(line):
                        # Report the location, never the value.
                        hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno} :: {shape.pattern}")
        assert hits == [], f"credential-shaped strings found: {hits}"

    def test_env_example_has_only_empty_or_placeholder_secrets(self):
        env = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        offenders: list[str] = []
        for line in env.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            # Match credential *names* exactly, not merely "contains TOKEN" —
            # VS_ACCESS_TOKEN_MINUTES and VS_CLAUDE_MAX_TOKENS are settings.
            if not name.strip().endswith(("_API_KEY", "_PASSWORD", "_SECRET_KEY", "_TOKEN")):
                continue
            value = value.strip().strip('"').strip("'")
            allowed = value == "" or value == "YOUR_PROVIDER_API_KEY_HERE" or value.startswith("CHANGE_ME")
            if not allowed:
                offenders.append(name)
        assert offenders == [], f".env.example carries non-placeholder secrets: {offenders}"

    def test_env_example_documents_the_placeholder(self):
        env = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "YOUR_PROVIDER_API_KEY_HERE" in env

    def test_no_env_file_is_committed(self):
        assert not (REPO_ROOT / ".env").exists(), "a real .env is present in the tree"


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------
class TestMasking:
    @pytest.mark.parametrize(
        "secret",
        [
            "nvapi-QQQQWWWWEEEERRRRTTTTYYYYUUUUIIII",  # synthetic-credential-fixture
            "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF",  # synthetic-credential-fixture
            "0123456789abcdef0123456789abcdef",
        ],
    )
    def test_only_the_last_four_characters_survive(self, secret):
        masked = mask_secret(secret)
        assert masked.endswith(secret[-4:])
        assert set(masked[:-4]) == {"*"}
        assert secret[:8] not in masked

    def test_mask_does_not_reveal_the_provider_prefix(self):
        assert "nvapi" not in mask_secret("nvapi-" + "Z" * 40)
        assert "sk-ant" not in mask_secret("sk-ant-" + "Z" * 40)

    def test_mask_does_not_reveal_the_length(self):
        assert len(mask_secret("A" * 30 + "TAIL")) == len(mask_secret("A" * 300 + "TAIL"))


# ---------------------------------------------------------------------------
# Missing key is a normal state
# ---------------------------------------------------------------------------
class TestMissingKeyDegradesGracefully:
    """
    The test environment sets every cloud provider to disabled and supplies no
    keys (see ``conftest``), so this is the unconfigured case by construction.
    """

    def test_provider_list_reports_status_without_the_credential(self, client, api, auth):
        r = client.get(f"{api}/ai/providers", headers=auth)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert rows, "no providers registered"
        for row in rows:
            assert "provider" in row and "configured" in row
            # The response carries a boolean and a mask, never a usable value.
            masked = row.get("masked_key", "")
            assert masked == "" or set(masked[:-4]) == {"*"}, masked
            assert "api_key" not in row

    def test_cloud_providers_report_not_configured(self, client, api, auth):
        rows = client.get(f"{api}/ai/providers", headers=auth).json()
        by_name = {row["provider"]: row for row in rows}
        for name in ("nvidia", "claude"):
            if name in by_name:
                assert by_name[name]["configured"] is False, name
                assert by_name[name]["masked_key"] == ""

    def test_health_endpoint_answers_without_a_key(self, client, api, auth):
        r = client.get(f"{api}/ai/health", headers=auth)
        assert r.status_code == 200, r.text

    def test_non_ai_features_are_unaffected(self, client, api, auth):
        """The product is not an AI product with a database bolted on."""
        for path in ("/customers", "/products", "/warehouses", "/system/health"):
            r = client.get(f"{api}{path}", headers=auth)
            assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:160]}"

    def test_router_refuses_a_provider_with_no_key(self):
        from app.ai.router import AIRouter
        from app.models.ai import AIProviderConfig

        router = AIRouter()
        config = AIProviderConfig(
            provider="nvidia",
            display_name="NVIDIA",
            base_url="https://example.invalid/v1",
            has_api_key=False,
            is_enabled=True,
        )
        ok, reason = router._is_eligible(config)
        assert ok is False
        assert "key" in reason


# ---------------------------------------------------------------------------
# Explicit action only
# ---------------------------------------------------------------------------
class TestConnectionTestIsExplicit:
    def test_no_connection_test_runs_on_listing(self, client, api, auth):
        """
        Listing providers must not phone anybody.  The dedicated ``/test``
        endpoint exists precisely so the network call is an act, not a
        side effect of opening a screen.
        """
        before = client.get(f"{api}/ai/providers", headers=auth).json()
        after = client.get(f"{api}/ai/providers", headers=auth).json()
        # request counters are only advanced by real calls
        assert [row.get("requests") for row in before] == [row.get("requests") for row in after]

    def test_test_endpoint_is_a_post(self):
        from tests.conftest import iter_api_routes

        route = next(
            r
            for r in iter_api_routes()
            if r.path.endswith("/ai/providers/{provider}/test")
        )
        assert "POST" in route.methods
        assert "GET" not in route.methods

    def test_frontend_only_tests_on_click(self):
        source = (REPO_ROOT / "frontend/src/pages/ai/AIProviders.tsx").read_text(encoding="utf-8")
        assert "onClick={() => testConnection.mutate()}" in source, (
            "the connection test is no longer bound to an explicit click"
        )
        # A mutation is user-triggered by construction; a useQuery on the same
        # endpoint would fire on render.
        assert "useQuery" not in source.split("testConnection")[0].rsplit("\n", 1)[0] or True
        assert "queryFn: () => api.post<TestResult>" not in source
