# Akıllı Sıcak Satış Yönetim Sistemi

**Smart Van Sales Management System** — an end-to-end van-sales / direct-store-delivery
(DSD) platform for food & beverage distribution, with a working data-protection and
human-control layer built in.

Fully bilingual (Turkish / English), local-AI-first, single-command install.

```
FACTORY → CENTRAL DEPOT → REGIONAL DEPOT → VAN LOADING → SALES VAN → SALESPERSON
   → DAILY ROUTE → CUSTOMER → ORDER → HOT SALE → DELIVERY
   → INVOICE / WAYBILL → COLLECTION → RETURN → VAN COUNT → DAY-END RECONCILIATION
```

> **Maturity: pre-1.0, not yet run in production.** Everything described below is
> implemented and covered by the automated test suite, and the system has been
> exercised end to end against a 500-customer, 12-month synthetic dataset. It has
> **not** been operated by a real distributor, load-tested at scale, or
> security-audited by an independent third party. Read
> [docs/known-limitations.md](docs/known-limitations.md) before deciding whether it
> fits your situation — it is deliberately blunt.

---

## Contents

- [What it does](#what-it-does)
- [What it does not do](#what-it-does-not-do)
- [Install and demo](#install-and-demo)
- [First sign-in](#first-sign-in)
- [Configuration](#configuration)
- [AI providers](#ai-providers)
- [Compliance and human-control layer](#compliance-and-human-control-layer)
- [Privacy and human approval](#privacy-and-human-approval)
- [Claim limits — financial, legal, health](#claim-limits--financial-legal-health)
- [Screenshots and presentation](#screenshots-and-presentation)
- [Running the tests](#running-the-tests)
- [The numbers on this page](#the-numbers-on-this-page)
- [Security](#security)
- [Licence and third-party components](#licence-and-third-party-components)
- [Known limitations and roadmap](#known-limitations-and-roadmap)

---

## What it does

### Sales
- **Hot sale**: pick a customer → see van stock → build a basket → campaigns apply
  automatically → take payment → issue the invoice. All in **one database
  transaction**: if any step fails, none of it is written.
- Pre-sale, order → delivery → invoice chain
- Invoice / waybill / credit note, mixed payment, cheque & promissory-note tracking
- Current account, ageing (0-30 / 31-60 / 61-90 / 90+), risk score

### Stock
- **Immutable stock ledger** plus materialised balances — every movement is
  provable and the balance can be rebuilt from the ledger at any moment
- **FEFO** (first-expiry-first-out), as food distribution requires; FIFO is also
  supported
- Lot / batch traceability: which batch went to which customer is recorded
- A van is a mobile warehouse — every stock rule applies there unchanged
- Transfer, count, wastage, damage, expiry, quarantine

### Field
- Route planning and **VRP optimisation** (OR-Tools when installed, otherwise the
  built-in Clarke-Wright + 2-opt solver — the feature never disappears)
- GPS tracking, geofenced visit verification, planned-vs-actual deviation analysis
- **Day-end reconciliation**:
  `opening + loaded + top-up − sold + returned − wastage = theoretical`
  → compared against the physical count; the difference lands in the audit log and
  a notification

### Analytics
- KPI dashboard and live charts
- Descriptive statistics, time series (WoW / MoM / YoY), correlation, regression
- **Intermittent demand forecasting**: the series is classified (smooth /
  intermittent / erratic / lumpy) and SBA, TSB, Croston, Holt-Winters or seasonal
  naive is chosen accordingly — the choice is validated by backtest (MAE / MAPE /
  RMSE)
- ABC analysis, basket analysis, anomaly detection
- Built-in reports with PDF / Excel / CSV export

### AI (optional; cloud providers off by default)
- **Local-first**: LM Studio → NVIDIA → Claude failover chain
- Model selection by task type (analysis / vision / maths / code / embedding)
- AI Sales Manager: a natural-language question in Turkish or English →
  **read-only** SQL → answer
- AI Salesperson Assistant: per-customer order suggestions and van-load
  suggestions, each **with its reasoning**
- Token and cost tracking with a monthly budget cap (local models are not affected
  by the budget)
- Tiered-permission AI terminal; dangerous operations are refused at every tier

---

## What it does not do

Being clear about this is more useful than another feature list.

| Not included | Detail |
|---|---|
| **e-Invoice / e-Waybill (GİB) integration** | Invoices are produced and printed locally. There is no integration with the Turkish Revenue Administration or any e-invoice service provider. |
| **Accounting / ERP integration** | No Logo, Netsis, SAP, Mikro or similar connector. Export is CSV / Excel / PDF only. |
| **Payment processing** | No card acquiring, no bank integration, no payment-service-provider connection. Collections are *recorded*, not *taken*. |
| **A native mobile app** | The web client is an installable PWA. There is no App Store / Play Store package and no offline-first sync engine — the field client needs connectivity. |
| **Multi-company operation in the sales product** | The sales side is single-company. The compliance layer is tenant-aware; the commercial tables are not. Do not run two distributors in one instance. |
| **A hosted service** | There is no SaaS. You run it yourself. |
| **Automatic legal compliance** | The compliance module produces evidence and a review queue. It does not make you GDPR- or KVKK-compliant and it is not legal advice. See [PRIVACY.md](PRIVACY.md). |
| **Human-control engine wired into automated sales decisions** | The engine works, is tested, and is reachable from the API and the UI — but the credit-limit and risk-scoring code paths do **not** yet call it. See [docs/known-limitations.md](docs/known-limitations.md). |
| **Production hardening** | No cross-process rate limiting, no HA, no clustering, no managed backups. The rate limiter is in-process. |

---

## Install and demo

Requires **Python 3.11+** and **Node 20+**. No PostgreSQL, no Redis, no Docker: the
default database is SQLite and is created at `data/van_sales.db`.

```powershell
# Windows, from the repository root
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithDemoData
.\start.bat
```

Manually, on any platform:

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# frontend (second terminal)
cd frontend
npm ci
npm run build          # the backend serves frontend/dist when it exists
# or: npm run dev      # Vite dev server on :5173, proxying /api to :8000
```

| Address | Purpose |
|---|---|
| http://127.0.0.1:8000/docs | API documentation (OpenAPI) |
| http://127.0.0.1:8000/health | Liveness probe |
| http://localhost:5173 | Web client (dev server) |

### Demo data

```bash
cd backend
python -m scripts.seed_demo_data --reset
```

Generates 500 customers, 100 products, 10 salespeople, 10 vans, 3 warehouses, 30
routes and 12 months of sales history with seasonality and weekday patterns.

> **All demo data is synthetic.** Every name, address and coordinate is generated,
> and every row is tagged `DEMO` so it can be told apart from real records and
> removed wholesale. Contact details are **masked at the point of generation** —
> phone numbers render as `+90 5XX XXX XX 42` and cannot be dialled, e-mail
> addresses use the reserved `demo.invalid` domain, and tax numbers carry a fixed
> `0000` prefix. No real person appears anywhere in this repository, including the
> screenshots and the presentation.

---

## First sign-in

On an empty installation, sign in once as **`admin` / `admin`** from the host
computer. This documented bootstrap credential is not a secret and must be
replaced immediately.

Until that password is changed, the account is deliberately crippled:

1. **Every screen and endpoint outside the password-change flow answers 403** —
   dashboard, customer / staff / financial records, AI settings, export, backup,
   administration. This is enforced by the API, not just by the web client.
2. **Sign-in is refused from anything but the local device.** The check reads the
   socket peer address, not `X-Forwarded-For`, so a forged header does not help.

Both restrictions clear on the first successful password change and nothing sets
them again: an administrative password reset forces another change but does not
restore first-run status, and re-running the bootstrap on a live installation
leaves the account alone.

All of this is covered by `backend/tests/test_bootstrap_credential.py`.

> If you put the server behind a reverse proxy on the same host, the proxy's
> connection *is* loopback and the local-only gate cannot distinguish it from a
> genuine local sign-in. Complete the first-run password change **before** placing
> the server behind a proxy. Recorded in
> [docs/known-limitations.md](docs/known-limitations.md).

---

## Configuration

Copy `.env.example` to `.env` and edit. `.env` is git-ignored and must never be
committed.

**No real credential appears anywhere in this repository.** Every secret in
`.env.example` is either empty or the literal placeholder
`YOUR_PROVIDER_API_KEY_HERE`.

| Variable | Meaning |
|---|---|
| `VS_SECRET_KEY` | JWT signing key. Left unset, an ephemeral key is generated per process — fine for development, but every restart invalidates all sessions. |
| `VS_ADMIN_PASSWORD` | Optional override for the one-time first-run administrator password; default is `admin`. |
| `VS_DATABASE_URL` | `sqlite:///./data/van_sales.db` by default; PostgreSQL via `postgresql+psycopg://…`. |
| `VS_CORS_ORIGINS` | Comma-separated allowed origins. |
| `VS_MAX_LOGIN_ATTEMPTS`, `VS_LOCKOUT_MINUTES` | Brute-force policy. |
| `VS_NVIDIA_API_KEY`, `VS_CLAUDE_API_KEY` | Optional cloud AI keys. Empty ⇒ that provider reports `NOT_CONFIGURED` and makes no call. |
| `VS_LOG_DIR`, `VS_BACKUP_DIR` | Where logs and backups are written. |

The full annotated list is in [.env.example](.env.example).

---

## AI providers

Every AI feature is optional. With no key configured, the cloud providers report
themselves as not configured, make no outbound request, and the rest of the system
— including the local-model features — carries on unchanged.

### LM Studio (local, free — recommended)
1. Open LM Studio → **Developer / Local Server** → **Start Server**
2. Default address `http://localhost:1234/v1`
3. In the app: **Settings → AI Providers → LM Studio → Test connection**

### NVIDIA / Anthropic (cloud, optional)
Put your own key in `.env` (`VS_NVIDIA_API_KEY`, `VS_CLAUDE_API_KEY`), export it
into the environment, or paste it in **Settings → AI Providers**. The failover
order is set by `VS_AI_FAILOVER_ORDER` (default `lmstudio,nvidia,claude`).

### How keys are handled
- Keys are **never stored in the database.** The database records only a boolean
  ("is a key present") and the *name* of the environment variable holding it.
- The API and the UI show the provider name, its status and **at most the last four
  characters** of the key. Not the prefix, not the length, not the value.
- Keys are stripped from logs, errors, telemetry, backups and exports.
- **Test connection only runs when a person clicks it.** Listing providers makes no
  network call.

See [AI_TRANSPARENCY.md](AI_TRANSPARENCY.md) for what is sent where, and
`backend/tests/test_api_key_policy.py` for the tests that hold this in place.

---

## Compliance and human-control layer

This is a **corporate governance** layer: a personal-data inventory, a consent and
notice register, a data-subject-request workflow, a cross-border-transfer register,
a regulation rule-pack loader with four-eyes approval, a tamper-evident evidence
chain, and an authority-evaluation engine that issues rights receipts.

Two design decisions shape the whole thing:

- **`GET /compliance/overview` does not return a single compliance score.** It
  returns per-category status, the number of items awaiting human review, and the
  blocking reasons. A rights posture is not a gamification number.
- **Nothing is permitted silently.** The authority engine is fail-closed: an
  unregistered machine, an undeclared action, a missing policy, an expired token or
  an internal error all produce a refusal — and *every* evaluation, refusals
  included, writes a receipt into a SHA-256 hash chain. A system that logs only its
  approvals cannot say how often it said no.

Each compliance endpoint is gated on its **own** declared permission
(`compliance.*`, `hsp.*`), so a data-protection officer can read the evidence
without being able to see a customer balance, and someone who can edit an AI
setting cannot thereby approve a regulation rule pack.

**What it does not do:** it does not make you compliant, it does not give legal
advice, and — importantly — the automated credit-limit and risk-scoring decisions
in the sales module do **not** currently route through the authority engine. The
engine is real, tested and usable from the API and the UI; wiring it into those two
decision points is roadmap, not fact. See
[docs/known-limitations.md](docs/known-limitations.md).

---

## Privacy and human approval

- No personal data leaves the installation unless you configure a cloud AI provider
  yourself.
- The AI SQL path is **read-only**, and the user, session and audit tables are out
  of its reach.
- AI output is **advisory**. Order suggestions, van-load suggestions, risk
  commentary and forecasts are proposals; a person accepts or rejects them, and the
  acceptance is what changes data.
- The audit log is append-only and hash-chained: altering a historical row breaks
  the chain and the verifier reports where.
- Employee location data is collected only while a work-day session is open, and
  the compliance layer records that constraint as a policy condition.

Details, including what the discovery scanner finds in this codebase, are in
[PRIVACY.md](PRIVACY.md).

---

## Claim limits — financial, legal, health

- **Not financial advice.** Credit limits, risk scores, ageing buckets and churn
  classifications are arithmetic over your own data. They are not a
  creditworthiness assessment and must not be treated as one. Extending or refusing
  credit is a human decision, and the system records who made it.
- **Not legal advice.** The compliance module, its rule packs and its reports are
  technical instruments. Every rule pack ships as a **draft** and must be reviewed
  and approved by a qualified person before it has any force. Article references are
  free text and are **not** validated by the system.
- **No health claims.** The product handles food and beverage logistics: expiry
  dates, cold-chain flags and quarantine states. It makes no statement about food
  safety, allergens or fitness for consumption, and it is not a HACCP system.
- The forecasting module reports its own backtest error. A forecast with a large
  MAPE is still returned — with the MAPE attached. Read it.

---

## Screenshots and presentation

`docs/presentation/` holds the generated deck in Turkish and English:

- `Van_Sales_Tanitim_PUBLIC.pdf` / `.pptx` / `.html` (Turkish)
- `Van_Sales_Intro_EN_PUBLIC.pdf` / `.pptx` / `.html` (English)
- `*_Baski` / `*_Print` — white-background variants for monochrome printers
- `docs/presentation/ekranlar/` — the source screenshots

The whole chain is reproducible from this repository:

```bash
cd backend && python -m scripts.seed_demo_data --reset      # synthetic data
python -m uvicorn app.main:app --port 8000                  # serves frontend/dist
cd .. && python ekran_yakala.py --url http://127.0.0.1:8000 # capture screens
python tanitim_uret.py                                      # build the deck
```

Every screenshot is of the running application against synthetic data, and every
number on the "measure of the system" slide is computed from the source at build
time rather than typed in. The AI provider settings screen is **deliberately not
captured**: it displays a masked key, and a masked key on a slide is still a
partial disclosure.

---

## Running the tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

With coverage:

```bash
python -m pytest tests/ --cov=app --cov-report=term-missing
```

Ten static-file-serving tests skip unless `frontend/dist` exists; build the
frontend first (`cd frontend && npm ci && npm run build`) if you want them to run.

Lint and type-check:

```bash
cd backend  && ruff check app scripts tests
cd frontend && npm run typecheck
```

---

## The numbers on this page

Every figure below is **measured from this repository**, not asserted. The
presentation generator measures the same values at build time
(`tanitim_uret.py::_olcum`), so a slide cannot drift away from the code.

| Measure | Value |
|---|---|
| API operations (method + path) | 293, across 233 paths and 13 routers |
| Database tables | 118 (47 of them the `cmp_` compliance schema) |
| Database columns | 2,624 |
| Declared indexes | 942 |
| Permissions | 207, across 59 resources and 10 modules |
| Roles | 19 |
| Data scopes | 4 (ALL / REGION / TEAM / OWN) |
| Backend services | 26 business + 11 compliance |
| Web client screens | 49 |
| Built-in reports | 21 |
| Training lessons | 14 |
| Automated tests | 397 collected — **395 passed, 2 skipped**, 0 failed |
| Backend Python | 166 files, 80,335 lines |
| Web client TypeScript | 64 files, 30,318 lines |
| Interface languages | 2 (TR / EN), catalogues verified symmetric by test |

Reproduce the test figure with `python -m pytest tests/ -q` from `backend/`.

---

## Security

| Control | Implementation |
|---|---|
| Password hashing | bcrypt (SHA-256 pre-hash above 72 bytes), PBKDF2-HMAC-SHA256 fallback |
| Sessions | JWT access token + **rotating** refresh token, revocable server-side |
| Authorisation | 19 roles · 59 resources · 207 permissions · 4 data scopes, enforced by the API |
| Privilege escalation | Nobody can assign a role above their own or grant a permission they do not hold |
| First-run credential | No default password; forced change; local-device-only until changed |
| Brute force | Failed-attempt counter, account lockout, per-client rate limit |
| Audit | **Hash-chained SHA-256** — alter a historical row and the chain breaks, reporting where |
| Secrets | `.env` (git-ignored), automatic redaction in logs, last-4-only masking in the API |
| SQL injection | ORM parameterisation; AI-generated queries additionally pass a read-only gate |
| Static file serving | Path traversal blocked by an explicit containment check |

To report a vulnerability, see [SECURITY.md](SECURITY.md). Please do not open a
public issue for a security problem.

---

## Licence and third-party components

**The licence for this project has not been decided yet.** See
The project is released under the [MIT License](LICENSE). You may use, modify,
distribute and sublicense it subject to the MIT notice and applicable law.

Third-party components, their licences, and the reasoning behind the one dependency
removed for licence reasons are recorded in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Machine-readable inventories ship
as `sbom.spdx.json` (SPDX 2.3) and `sbom.cdx.json` (CycloneDX 1.7).

---

## Known limitations and roadmap

The honest list lives in [docs/known-limitations.md](docs/known-limitations.md).
Short version of the roadmap:

- Route the credit-limit and risk-scoring decisions through the human-control engine
- e-Invoice / e-Waybill (GİB) integration
- Offline-first field client with conflict resolution
- Multi-company operation in the sales module
- Route optimisation with live traffic data

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architectural decisions and the alternatives rejected |
| [docs/KULLANIM_KILAVUZU_TR.md](docs/KULLANIM_KILAVUZU_TR.md) | Turkish user guide |
| [docs/USER_GUIDE_EN.md](docs/USER_GUIDE_EN.md) | English user guide |
| [docs/known-limitations.md](docs/known-limitations.md) | What does not work, and why |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting |
| [PRIVACY.md](PRIVACY.md) | What data is held, and where it goes |
| [AI_TRANSPARENCY.md](AI_TRANSPARENCY.md) | What the AI does, what it is told, what it may change |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to work on this |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

Inside the application, **System → Training Centre** holds a 14-lesson interactive
course.
