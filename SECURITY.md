# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**). If that is unavailable, open a public
issue containing only the words "security report — please open a private channel",
with no technical detail, and wait to be contacted.

Please include, as far as you can:

- what the problem is and what an attacker gains,
- the version or commit you tested,
- a minimal reproduction — a request, a payload, a sequence of steps,
- whether you have disclosed it anywhere else.

### What to expect

| Stage | Target |
|---|---|
| Acknowledgement | 3 working days |
| Initial assessment (is it a vulnerability, how severe) | 10 working days |
| Fix or a dated plan | 90 days from acknowledgement |

This project is maintained without a funded security team; these are targets held
in good faith, not a contractual SLA. If a deadline slips you will be told, with a
reason.

Credit is given in the changelog unless you ask otherwise.

---

## Scope

**In scope** — the code in this repository:

- authentication, session handling, the first-run credential gate
- the authorisation model (roles, permissions, data scopes)
- the AI SQL gate and the AI terminal permission tiers
- the static file server and any path handling
- the audit and evidence hash chains
- data exposure through the API, exports, logs or backups
- dependency vulnerabilities that are actually reachable from this code

**Out of scope**:

- vulnerabilities in a deployment you control (your reverse proxy, your OS, your
  TLS configuration) unless this project's documentation told you to configure it
  that way
- findings that require an already-compromised host or an already-privileged
  account, with no privilege boundary crossed
- missing hardening headers with no demonstrated impact
- automated-scanner output with no analysis attached
- social engineering, physical access, denial of service by brute volume
- the known limitations already documented in
  [docs/known-limitations.md](docs/known-limitations.md) — those are recorded, not
  hidden; a report showing one is *worse* than documented is very much in scope

---

## Supported versions

Pre-1.0. Only the tip of the default branch is supported. There are no backported
security releases.

---

## What is already in place

Reported so you can aim at something more interesting.

| Control | Where |
|---|---|
| No default password; forced change; local-device-only until changed | `app/services/bootstrap_service.py`, `app/core/deps.py`, `app/services/auth_service.py` |
| Password hashing: bcrypt with SHA-256 pre-hash above 72 bytes, PBKDF2 fallback | `app/core/security.py` |
| Rotating refresh tokens, server-side revocation | `app/services/auth_service.py` |
| Per-resource, per-action authorisation with four data scopes | `app/core/permissions.py`, `app/core/deps.py` |
| Privilege-escalation guards on role assignment and permission grants | `app/services/auth_service.py` |
| Account lockout and per-client rate limiting | `app/services/auth_service.py`, `app/core/middleware.py` |
| Hash-chained audit log and evidence chain, with verifiers | `app/services/audit_service.py`, `app/compliance/services/evidence_service.py` |
| Read-only SQL gate for AI-generated queries | `app/ai/sql_guard.py` |
| Secret redaction in logs; last-4-only masking in API responses | `app/core/logging_config.py`, `app/core/security.py` |
| Path-traversal containment on the SPA fallback route | `app/main.py` |
| Security headers (nosniff, frame options, referrer policy, HSTS in production) | `app/core/middleware.py` |

Each has regression tests under `backend/tests/`.

---

## Hardening notes for operators

1. **Set `VS_SECRET_KEY`.** Left unset, an ephemeral key is generated per process
   and every restart invalidates all sessions.
2. **Complete the first-run password change before putting the server behind a
   reverse proxy.** A same-host proxy connects from loopback, which the
   local-only gate cannot distinguish from a genuine local sign-in.
3. **Terminate TLS in front of the application.** It speaks plain HTTP.
4. **Restrict `VS_CORS_ORIGINS`** to the origins you actually serve.
5. **Treat `.env` as a secret.** It is git-ignored, and the application writes
   provider keys into it with `0600` where the platform supports it.
6. **Back up, and test the restore.** Backups are SHA-256 stamped and the restore
   path verifies before overwriting, but an untested backup is not a backup.
7. **Run `pip-audit` and `npm audit` on your own schedule.** The pins in this
   repository were clean on the release date; that is a point in time, not a
   guarantee.
