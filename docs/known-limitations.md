# Known limitations

The things this system does not do, does badly, or does only under conditions that
are easy to miss. Kept blunt on purpose: a limitation you find here costs you
nothing, and a limitation you find in production costs you a lot.

Each entry says what the situation actually is, and — where relevant — what would
have to change.

---

## Product scope

### No e-Invoice / e-Waybill (GİB) integration
Invoices, waybills and credit notes are produced, numbered and printed locally.
There is no connection to the Turkish Revenue Administration or to any e-invoice
service provider. If you are legally required to issue e-invoices, this system does
not do it for you.

### No accounting or ERP connector
No Logo, Netsis, SAP, Mikro or similar integration. Data leaves as CSV, Excel or
PDF and someone imports it at the other end.

### Collections are recorded, not taken
There is no card acquiring, no bank integration, no payment-service-provider
connection. "Collection" means a person received money and recorded that fact.

### The sales product is single-company
The compliance layer is tenant-aware (`cmp_tenants`, tenant-scoped evidence and
receipt chains). **The commercial tables are not.** There is no company-level
isolation on customers, sales, stock or users. Do not run two distributors in one
instance and expect them not to see each other.

### No offline-first field client
The web client is an installable PWA and caches its own assets, but it needs
connectivity to work. There is no offline queue, no conflict resolution and no
background sync. A salesperson in a dead spot cannot record a sale. The data model
anticipates this — every document carries a client-generated `client_uid` that is
unique, so idempotent replay is possible — but the client that would use it does
not exist.

---

## Human-control layer (HSP)

### The engine is real; two decisions do not use it yet
This is the most important entry on this page.

The authority-evaluation engine works. It is fail-closed, it writes a
hash-chained receipt for every decision including every refusal, and it is covered
by 32 tests (`backend/tests/test_hsp_engine.py`) that fix default-deny, expiry,
revocation, human approval, emergency override and chain-tamper detection.

**But the sales module's two automated decisions do not call it.**
`customer_service.check_credit` refuses a sale over the credit limit, and the risk
score is computed, without consulting the engine and without producing a rights
receipt. Both are recorded in the ordinary audit log.

The engine is reachable from the API (`POST /compliance/hsp/evaluate`) and from the
Rights Receipts screen, and the seeded policies already describe those two decision
points — so the declarations exist and the wiring does not.

**Why it was not simply wired in:** the engine is fail-closed by design. An
installation that has not configured the compliance tenant would have every
credit-limited sale refused. Making it fail-open for unconfigured installations
would contradict the engine's central property. Doing this properly means an
explicit, operator-visible enablement step, and that is a design decision the owner
has not taken yet.

### The appeal route is a data-subject request, not a separate workflow
Appealing a decision (`POST /compliance/hsp/receipts/{id}/appeal`) files a request
of type `AUTOMATED_DECISION_REVIEW` in the existing data-subject-request pipeline
and links it to the receipt. The receipt itself is never modified — the receipt
table is append-only by design, and rewriting a decision after the fact would
destroy the chain's meaning. One appeal per receipt; a second is refused.

### Rights policies are seeded, not authored
`hsp_seed` registers the product's declared decision points with starter policies.
Every one of them ships with review notes attached: the lawful basis is
`REVIEW_REQUIRED`, the proportionality of location tracking is unassessed, and the
operational meaning of the appeal route (who answers, in what period) is undefined.
They are a starting point for a human, not a finished policy set.

---

## Compliance layer

### No automatic retention enforcement
Nothing is deleted on a schedule. The retention-policy tables exist; the job that
acts on them does not. `gps_events` in particular accumulates without limit, and
employee location traces are exactly the data that most needs a retention period.
Deleting old data is currently a manual operation.

### Deadlines are recorded, not computed
A data-subject request with no due date is displayed as "period unknown", never as
"not overdue". The software does not know which regime applies to you, and guessing
would produce a confidently wrong date. Someone has to set it.

### Rule packs are drafts
The GDPR and KVKK packs load with status `DRAFT` and require four-eyes approval
before they have any force. Article references are free text and are **not**
validated by the system. They are not legal advice and were not written by a
lawyer.

### Field classification needs a human
The discovery scanner finds fields and proposes a sensitivity. Everything lands in
`REVIEW_REQUIRED`. On a default installation that is 81 fields waiting for someone
to confirm or correct them. The scanner is deliberately conservative about
special-category data — it reports 0 candidates on this codebase, and that is a
measurement, not a guarantee.

---

## Security

### The first-run local-only gate cannot see through a same-host proxy
Until the initial password is changed, sign-in is refused unless the connection
originates on the loopback interface. The check reads the socket peer address and
ignores `X-Forwarded-For`, so a forged header does not defeat it — but a reverse
proxy running on the same host *is* loopback, and the gate cannot distinguish it
from a genuine local sign-in.

**Complete the first-run password change before placing the server behind a
proxy.**

### The rate limiter is in-process
Sliding-window counters live in the application process. Run two workers and each
gets its own budget; restart and the window resets. It raises the cost of a naive
brute-force attempt; it is not a defence against a distributed one. Account lockout
is the control that actually holds, and that one is in the database.

### No TLS
The application speaks plain HTTP. Terminate TLS in front of it.

### `VS_SECRET_KEY` defaults to ephemeral
Unset, a random key is generated per process. Convenient for development; in
production every restart invalidates every session. Set it.

### Not independently audited
The security controls listed in [SECURITY.md](SECURITY.md) are implemented and have
regression tests. No third party has reviewed them.

---

## AI

### Local model quality is your problem
The LM Studio path is only as good as the model you load. A small model will write
poor SQL and give poor suggestions. The SQL gate stops it doing damage; it does not
make it right.

### Cost figures are estimates
Token counts are recorded and cost is computed from the configured per-1k rates.
That is a budgeting aid, not a bill. Reconcile against your provider's statement.

### The AI features are not evaluated
There is no accuracy benchmark for the suggestions, no eval set for the
natural-language-to-SQL path, and no regression test that the answers are *correct*
— only that the machinery behaves (the gate refuses writes, budgets are enforced,
missing keys degrade cleanly).

---

## Presentation and documentation

### The shipped PDFs embed Verdana and Arial, not the font the source specifies
`tanitim_uret.py` specifies **DejaVu Sans**, a libre typeface with full Turkish
coverage. PowerPoint can only embed fonts installed on the build machine, and
DejaVu Sans was not installed on the machine that produced the shipped PDFs, so it
silently substituted: the files embed subsets of **Verdana** and **Arial**.

Install DejaVu Sans before running `tanitim_uret.py` to reproduce the deck as
intended. Details and a verification snippet in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

### Deck generation requires Microsoft PowerPoint
`tanitim_uret.py` builds the `.pptx` with python-pptx, then drives PowerPoint via
COM to export PDF and PNG, and builds the HTML deck from those PNGs. Without
PowerPoint the PPTX is still produced and the PDF/HTML steps are skipped with a
message. There is no LibreOffice fallback.

### Screenshot capture needs a running server with demo data
`ekran_yakala.py` drives a real browser against a live instance. It needs the
frontend built, the backend serving it, the demo dataset seeded, and — easy to miss
— **an administrator account that has completed the first-run password change**.
An account still owing a password change cannot open any screen, so capturing with
one produces a deck of empty pages.

### The AI provider settings screen is deliberately never captured
It renders a masked key. A masked key on a slide still discloses the last four
characters and the fact that a key exists. If that screen ever needs to appear, it
must be captured on an installation with no key configured.

---

## Testing

### 397 tests, and what they do not cover
The suite covers the domain logic, the security controls, the compliance layer, the
UI/API contract and the demo-data privacy rules. It does **not** include:

- browser-level end-to-end tests (the screenshot script is the closest thing, and
  it is not assertive)
- load or performance tests
- PostgreSQL — every test runs on SQLite, so PostgreSQL support is *supported* in
  the sense that the ORM abstracts it, not in the sense that it is tested
- concurrency and race conditions
- the AI providers against live endpoints (tests run with all providers disabled)

### Two tests skip by default
Ten static-file-serving tests require `frontend/dist`; build the frontend to run
them. The reported figure of 395 passed / 2 skipped is from a run **with** the
frontend built.

---

## Roadmap

Rough order of usefulness, not a schedule:

1. Route the credit-limit refusal and the risk score through the authority engine,
   with an explicit enablement step
2. Retention enforcement job acting on the existing policy tables
3. e-Invoice / e-Waybill (GİB) integration
4. Offline-first field client using the existing `client_uid` idempotency
5. Multi-company isolation in the sales module
6. PostgreSQL in CI, alongside SQLite
7. Route optimisation with live traffic data
