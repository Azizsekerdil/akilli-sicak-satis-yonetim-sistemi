"""
Permission policy for the in-app AI development terminal.

Two independent gates stand between an instruction and the operating system:

1. **Hard blocks** — a fixed list of actions that are never executable, at any
   permission tier, with or without approval: disk formatting, recursive
   deletion outside the project, power/registry/service changes, disabling
   Defender or the firewall, credential-store access, writing outside the
   project tree, printing ``.env``, and download-and-execute one-liners.
2. **Permission tiers** — :class:`~app.core.enums.AIPermissionLevel`, ordered by
   ``AI_PERMISSION_ORDER``.  A session may only perform actions at or below its
   own tier, and ``SYSTEM_COMMAND`` additionally requires an explicit,
   per-command approval token that the user has to send back.

Everything here is a pure decision function: nothing in this module writes to
the database or the audit log.  :mod:`app.services.ai_service` records the
outcome of every decision — allowed, blocked or pending approval alike.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import PROJECT_ROOT, settings
from app.core.enums import AI_PERMISSION_ORDER, AIPermissionLevel
from app.core.logging_config import get_logger, redact

log = get_logger("app.ai.terminal")

#: Everything the terminal may touch lives under here.  Resolved once so a
#: symlinked or relative path cannot smuggle its way out.
PROJECT_DIR: Path = Path(PROJECT_ROOT).resolve()

MAX_OUTPUT_CHARS = 8000
MAX_READ_BYTES = 512_000

DEFAULT_TIMEOUTS: dict[str, int] = {
    "RUN_TESTS": 600,
    "PACKAGE_INSTALL": 600,
    "GIT": 120,
    "SHELL": 120,
}


# ===========================================================================
# Action vocabulary
# ===========================================================================
class ActionType:
    """Terminal action kinds (stored in ``ai_terminal_commands.action_type``)."""

    READ_FILE = "READ_FILE"
    LIST_DIR = "LIST_DIR"
    WRITE_FILE = "WRITE_FILE"
    RUN_TESTS = "RUN_TESTS"
    PACKAGE_INSTALL = "PACKAGE_INSTALL"
    GIT = "GIT"
    SHELL = "SHELL"


#: Minimum tier required for each action kind.
ACTION_LEVEL: dict[str, str] = {
    ActionType.READ_FILE: str(AIPermissionLevel.READ_ONLY),
    ActionType.LIST_DIR: str(AIPermissionLevel.READ_ONLY),
    ActionType.WRITE_FILE: str(AIPermissionLevel.PROJECT_WRITE),
    ActionType.RUN_TESTS: str(AIPermissionLevel.RUN_TESTS),
    ActionType.PACKAGE_INSTALL: str(AIPermissionLevel.PACKAGE_INSTALL),
    ActionType.GIT: str(AIPermissionLevel.GIT_OPERATIONS),
    ActionType.SHELL: str(AIPermissionLevel.SYSTEM_COMMAND),
}

#: Git subcommands the GIT tier covers.  Anything else (push, reset, clean,
#: rebase…) falls through to SYSTEM_COMMAND and therefore needs approval.
GIT_ALLOWED = frozenset(
    {"status", "diff", "add", "commit", "log", "show", "branch", "rev-parse", "stash"}
)

#: pip subcommands the PACKAGE_INSTALL tier covers.  ``uninstall`` is excluded
#: deliberately: removing a dependency can break the running system.
PIP_ALLOWED = frozenset({"install", "list", "show", "freeze", "check", "download"})


# ===========================================================================
# Hard blocks
# ===========================================================================
#: (pattern, reason).  Matched case-insensitively against the whole request.
HARD_BLOCK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bformat\s+[a-z]:", "disk format"),
    (r"\bmkfs(\.\w+)?\b", "filesystem creation"),
    (r"\bdiskpart\b", "disk partitioning"),
    (r"\bfsutil\b", "low-level filesystem control"),
    (r"\bbcdedit\b", "boot configuration change"),
    (r"\bshutdown\b", "power state change"),
    (r"\b(restart|stop)-computer\b", "power state change"),
    (r"\breg\s+delete\b", "registry deletion"),
    (r"\bremove-item\b[^\n]*\bhk(lm|cu):", "registry deletion"),
    (r"\bnew-itemproperty\b[^\n]*\bhklm:", "registry modification"),
    (r"\b(set|add)-mppreference\b", "Windows Defender configuration"),
    (r"\bdisablerealtimemonitoring\b", "disabling Defender"),
    (r"\bnetsh\s+advfirewall\b", "firewall configuration"),
    (r"\bset-netfirewall\w*\b", "firewall configuration"),
    (r"\bsc\s+(stop|delete|config)\b", "service control"),
    (r"\b(stop|remove|set)-service\b", "service control"),
    (r"\bwindefend\b|\bwindowsdefender\b", "Windows Defender control"),
    (r"\bcmdkey\b", "credential store access"),
    (r"\bvaultcmd\b", "credential store access"),
    (r"\bget-credential\b", "credential prompt"),
    (r"\blsass\b|\bmimikatz\b|\bsekurlsa\b", "credential dumping"),
    (r"\bnet\s+user\b|\bnet\s+localgroup\b", "account management"),
    (r"\bnew-localuser\b|\badd-localgroupmember\b", "account management"),
    (r"\brunas\b", "privilege elevation"),
    (r"-verb\s+runas", "privilege elevation"),
    (r"\btakeown\b|\bicacls\b|\bcacls\b", "ownership or ACL change"),
    (r"\bschtasks\b|\bregister-scheduledtask\b|\bcrontab\b", "scheduled task creation"),
    (
        r"\b(curl|wget|invoke-webrequest|iwr|invoke-restmethod|irm)\b[^\n]*\|[^\n]*"
        r"\b(iex|invoke-expression|bash|sh|powershell|pwsh|cmd|node|python)\b",
        "download-and-execute",
    ),
    (r"\b(iex|invoke-expression)\b", "dynamic code execution"),
    (r"-(enc|encodedcommand)\b", "encoded command"),
    (r"\bpowershell\b[^\n]*-nop[^\n]*-w\s+hidden", "hidden shell"),
    (r"\b(type|cat|more|get-content|gc|less|head|tail)\b[^\n]*\.env\b", "printing .env"),
    (r"\.env\b[^\n]*\|\s*(select|findstr|grep|out-)", "printing .env"),
    (r"\bhistory\b[^\n]*\|\s*(grep|findstr)", "shell history mining"),
    (r"\bdd\s+if=", "raw device write"),
    (r">\s*/dev/sd[a-z]", "raw device write"),
)

_COMPILED_BLOCKS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in HARD_BLOCK_PATTERNS
)

#: Recursive/forced deletion verbs.  Allowed only when every path they touch is
#: inside the project, and even then only as an approved SYSTEM_COMMAND.
_DESTRUCTIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\b|\brm\s+-[a-z]*f[a-z]*r\b", re.I), "rm -rf"),
    (re.compile(r"\b(rd|rmdir)\s+/s\b", re.I), "rd /s"),
    (re.compile(r"\bdel\s+/[a-z]*[fs]\b", re.I), "del /f /s"),
    (re.compile(r"\bremove-item\b[^\n]*-recurse\b", re.I), "Remove-Item -Recurse"),
    (re.compile(r"\bgit\s+clean\s+-[a-z]*f", re.I), "git clean -f"),
)

#: Paths that are always off limits, even for a read.
_SYSTEM_PATH_HINTS: tuple[str, ...] = (
    "c:\\windows", "c:/windows", "c:\\program files", "c:/program files",
    "c:\\programdata", "c:/programdata", "%systemroot%", "%windir%",
    "/etc/", "/usr/", "/bin/", "/sbin/", "/boot/", "/var/", "/dev/",
    "system32", "syswow64",
)

#: Environment variables never handed to a child process.
_SECRET_ENV_RE = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API)", re.IGNORECASE
)


@dataclass(slots=True)
class Decision:
    """The verdict on one requested terminal action."""

    action_type: str
    required_level: str
    allowed: bool = False
    requires_approval: bool = False
    block_reason: str | None = None
    command: str | None = None
    argv: list[str] = field(default_factory=list)
    target: str | None = None
    resolved_target: str | None = None
    content: str | None = None
    timeout: int = 120

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "required_level": self.required_level,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "block_reason": self.block_reason,
            "command": self.command,
            "target": self.resolved_target or self.target,
        }


@dataclass(slots=True)
class ExecutionResult:
    exit_code: int
    output: str
    duration_ms: int


# ===========================================================================
# Path safety
# ===========================================================================
def is_inside_project(path: str | Path) -> bool:
    """True when *path* resolves inside the project tree."""
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = PROJECT_DIR / candidate
        resolved = candidate.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return resolved == PROJECT_DIR or PROJECT_DIR in resolved.parents


def resolve_project_path(raw: str) -> Path | None:
    """Absolute path inside the project, or ``None`` when it escapes."""
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    return resolved if is_inside_project(resolved) else None


def is_env_file(path: str | Path) -> bool:
    """``.env`` and its variants hold live credentials and are never readable."""
    name = Path(path).name.lower()
    return name == ".env" or name.startswith(".env.")


def _path_like_tokens(text: str) -> list[str]:
    """Tokens from a command line that look like filesystem paths."""
    tokens: list[str] = []
    for raw in re.split(r"\s+", text or ""):
        token = raw.strip("\"'")
        if not token or token.startswith("-"):
            continue
        if re.match(r"^[a-zA-Z]:[\\/]", token) or token.startswith(("/", "\\", "~")):
            tokens.append(token)
        elif "/" in token or "\\" in token:
            tokens.append(token)
    return tokens


def _touches_system_path(text: str) -> str | None:
    lowered = (text or "").lower()
    for hint in _SYSTEM_PATH_HINTS:
        if hint in lowered:
            return hint
    return None


# ===========================================================================
# Permission tiers
# ===========================================================================
def level_rank(level: str) -> int:
    """Position of *level* in the escalation order; unknown tiers rank lowest."""
    try:
        return AI_PERMISSION_ORDER.index(str(level))
    except ValueError:
        return 0


def level_allows(session_level: str, required_level: str) -> bool:
    return level_rank(session_level) >= level_rank(required_level)


# ===========================================================================
# Approval tokens
# ===========================================================================
def make_approval_token(session_id: int, action_type: str, command: str) -> str:
    """
    Deterministic token the user must echo back to run a SYSTEM_COMMAND.

    Bound to the exact command text, so approving one command cannot be reused
    to run a different one.
    """
    payload = f"{session_id}|{action_type}|{(command or '').strip()}"
    return hmac.new(
        settings.secret_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]


def verify_approval_token(
    session_id: int, action_type: str, command: str, token: str | None
) -> bool:
    if not token:
        return False
    expected = make_approval_token(session_id, action_type, command)
    return hmac.compare_digest(expected, token.strip())


# ===========================================================================
# Classification
# ===========================================================================
def hard_block_reason(*parts: str | None) -> str | None:
    """First hard-block rule matched by any part of the request, if any."""
    haystack = " ".join(p for p in parts if p)
    if not haystack.strip():
        return None
    for pattern, reason in _COMPILED_BLOCKS:
        if pattern.search(haystack):
            return reason

    for pattern, label in _DESTRUCTIVE_PATTERNS:
        if not pattern.search(haystack):
            continue
        targets = _path_like_tokens(haystack)
        if not targets:
            return f"{label} without an explicit in-project path"
        outside = [t for t in targets if not is_inside_project(t)]
        if outside:
            return f"{label} outside the project ({outside[0]})"

    system_hint = _touches_system_path(haystack)
    if system_hint:
        return f"system path access ({system_hint})"
    return None


def _classify_shell(command: str) -> tuple[str, list[str]]:
    """Map a command line onto an action kind and its argv."""
    try:
        argv = shlex.split(command, posix=False)
    except ValueError:
        argv = command.split()
    argv = [a.strip('"') for a in argv if a.strip()]
    if not argv:
        return ActionType.SHELL, []

    head = Path(argv[0]).name.lower()
    rest = [a.lower() for a in argv[1:]]

    if head in ("pytest", "py.test") or (
        head in ("python", "python.exe", "py") and "-m" in rest and "pytest" in rest
    ):
        return ActionType.RUN_TESTS, argv
    if head == "pip" or (head in ("python", "python.exe", "py") and "pip" in rest):
        sub = next((a for a in rest if a in PIP_ALLOWED), None)
        return (ActionType.PACKAGE_INSTALL if sub else ActionType.SHELL), argv
    if head in ("git", "git.exe"):
        sub = rest[0] if rest else ""
        return (ActionType.GIT if sub in GIT_ALLOWED else ActionType.SHELL), argv
    return ActionType.SHELL, argv


def classify(
    *,
    requested_action: str | None,
    target: str | None,
    command: str | None,
) -> Decision:
    """
    Decide what kind of action this is and what tier it needs.

    Runs before any tier check so that a hard-blocked request is refused even
    for a ``SYSTEM_COMMAND`` session with a valid approval token.
    """
    action = (requested_action or "").strip().upper()
    blocked = hard_block_reason(command, target, action)

    if action in (ActionType.READ_FILE, ActionType.LIST_DIR):
        decision = Decision(
            action_type=action,
            required_level=ACTION_LEVEL[action],
            target=target,
            command=command,
            timeout=30,
        )
        resolved = resolve_project_path(target or "")
        if blocked:
            decision.block_reason = blocked
        elif resolved is None:
            decision.block_reason = "path is outside the project directory"
        elif is_env_file(resolved):
            decision.block_reason = "reading .env is never permitted"
        else:
            decision.resolved_target = str(resolved)
            decision.allowed = True
        return decision

    if action == ActionType.WRITE_FILE:
        decision = Decision(
            action_type=action,
            required_level=ACTION_LEVEL[action],
            target=target,
            content=command,
            timeout=30,
        )
        resolved = resolve_project_path(target or "")
        if blocked:
            decision.block_reason = blocked
        elif resolved is None:
            decision.block_reason = "writing outside the project directory is never permitted"
        elif is_env_file(resolved):
            decision.block_reason = "the .env file may not be written from the terminal"
        else:
            decision.resolved_target = str(resolved)
            decision.allowed = True
        return decision

    # Everything else is a command line.
    text = (command or "").strip()
    action_type, argv = _classify_shell(text)
    required = ACTION_LEVEL[action_type]
    decision = Decision(
        action_type=action_type,
        required_level=required,
        command=text,
        argv=argv,
        target=target,
        timeout=DEFAULT_TIMEOUTS.get(action_type, 120),
    )
    if not text:
        decision.block_reason = "empty command"
        return decision
    if blocked:
        decision.block_reason = blocked
        return decision
    if action_type == ActionType.SHELL:
        # Anything not recognised as a tests/pip/git operation is a system
        # command: highest tier, and always an explicit approval.
        decision.requires_approval = True
    decision.allowed = True
    return decision


def authorize(decision: Decision, session_level: str) -> Decision:
    """Apply the session's permission tier to an already-classified action."""
    if decision.block_reason:
        decision.allowed = False
        decision.requires_approval = False
        return decision
    if not level_allows(session_level, decision.required_level):
        decision.allowed = False
        decision.block_reason = (
            f"requires permission level {decision.required_level}, "
            f"session has {session_level}"
        )
    return decision


# ===========================================================================
# Execution
# ===========================================================================
def _child_env() -> dict[str, str]:
    """Environment for a child process, stripped of every credential-shaped key."""
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}


def _normalise_argv(decision: Decision) -> list[str]:
    """Resolve the interpreter for python-driven actions so PATH cannot decide."""
    argv = list(decision.argv)
    if not argv:
        return argv
    head = Path(argv[0]).name.lower()
    if head in ("python", "python.exe", "py"):
        argv[0] = sys.executable
    elif decision.action_type == ActionType.RUN_TESTS and head in ("pytest", "py.test"):
        argv = [sys.executable, "-m", "pytest", *argv[1:]]
    elif decision.action_type == ActionType.PACKAGE_INSTALL and head == "pip":
        argv = [sys.executable, "-m", "pip", *argv[1:]]
    return argv


def execute(decision: Decision) -> ExecutionResult:
    """
    Carry out an authorised action.

    Assumes :func:`classify` and :func:`authorize` have already approved it —
    calling this with a blocked decision is a programming error and raises.
    """
    if not decision.allowed:
        raise PermissionError(decision.block_reason or "action not allowed")

    started = time.perf_counter()

    if decision.action_type == ActionType.READ_FILE:
        path = Path(decision.resolved_target or "")
        if not path.is_file():
            return ExecutionResult(1, f"file not found: {path}", _ms(started))
        data = path.read_bytes()[:MAX_READ_BYTES]
        text = data.decode("utf-8", errors="replace")
        return ExecutionResult(0, redact(text)[:MAX_OUTPUT_CHARS], _ms(started))

    if decision.action_type == ActionType.LIST_DIR:
        path = Path(decision.resolved_target or "")
        if not path.is_dir():
            return ExecutionResult(1, f"directory not found: {path}", _ms(started))
        entries = sorted(
            f"{'d' if p.is_dir() else 'f'}  {p.name}" for p in path.iterdir()
        )
        return ExecutionResult(0, "\n".join(entries)[:MAX_OUTPUT_CHARS], _ms(started))

    if decision.action_type == ActionType.WRITE_FILE:
        path = Path(decision.resolved_target or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = decision.content or ""
        path.write_text(content, encoding="utf-8")
        return ExecutionResult(
            0, f"wrote {len(content)} characters to {path}", _ms(started)
        )

    argv = _normalise_argv(decision)
    if not argv:
        return ExecutionResult(1, "nothing to execute", _ms(started))
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, shell=False, tier-gated
            argv,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=decision.timeout,
            shell=False,
            env=_child_env(),
            check=False,
        )
    except FileNotFoundError:
        return ExecutionResult(127, f"command not found: {argv[0]}", _ms(started))
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            124, f"command timed out after {decision.timeout}s", _ms(started)
        )

    output = f"{completed.stdout or ''}{completed.stderr or ''}"
    return ExecutionResult(
        completed.returncode, redact(output)[:MAX_OUTPUT_CHARS], _ms(started)
    )


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = [
    "ActionType",
    "Decision",
    "ExecutionResult",
    "GIT_ALLOWED",
    "PIP_ALLOWED",
    "PROJECT_DIR",
    "authorize",
    "classify",
    "execute",
    "hard_block_reason",
    "is_inside_project",
    "level_allows",
    "level_rank",
    "make_approval_token",
    "resolve_project_path",
    "verify_approval_token",
]
