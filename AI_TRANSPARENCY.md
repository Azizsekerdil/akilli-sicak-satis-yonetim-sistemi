# AI transparency

What the AI features do, what they are told, what they may change, and what
happens when they are switched off.

---

## The one-line summary

Every AI feature is **optional and advisory**. Nothing an AI produces changes data
on its own: a person accepts a suggestion, and the acceptance is the write. With
no provider configured, the AI screens report themselves as unconfigured and the
rest of the product is unaffected.

---

## Providers

| Provider | Where it runs | Default | Needs a key |
|---|---|---|---|
| **LM Studio** | Your own machine (`localhost:1234`) | Enabled | No |
| **NVIDIA NIM** | NVIDIA's cloud | Enabled in config, inert without a key | Yes |
| **Anthropic Claude** | Anthropic's cloud | **Disabled** | Yes |

The failover order is `VS_AI_FAILOVER_ORDER`, default `lmstudio,nvidia,claude` —
local first, deliberately. A provider that is enabled but has no key is **not
eligible**: the router skips it and records why, rather than attempting a call that
would fail.

### Credentials

- API keys are **never stored in the database.** The database holds a boolean
  ("is a key present") and the *name* of the environment variable that holds it.
- Keys live in `.env` or the process environment. When you paste one into
  **Settings → AI Providers**, it is written to `.env` (mode `0600` where the
  platform supports it) and exported into the running process — never into a table.
- The API and the UI show the provider name, its status, and **at most the last
  four characters**. Not the prefix — an earlier version kept the first four, which
  leaked the vendor and key class into every screenshot; that was removed.
- Keys are stripped from logs, error responses, the audit trail, backups and
  exports.
- **"Test connection" runs only when a person clicks it.** Listing providers makes
  no network call.

Enforced by `backend/tests/test_api_key_policy.py`.

---

## The features, one by one

### 1. AI Sales Manager — natural-language questions over your data

**What it does.** You ask "which customers in Marmara fell more than 30 % last
month?" in Turkish or English. The model writes SQL, the SQL passes a gate, the
gate runs it, and the answer comes back with the query and the rows it came from.

**What the model is told.** The question, the database schema (table and column
names, not contents), and a small aggregate context.

**What it may change.** Nothing. The SQL gate (`app/ai/sql_guard.py`) permits
`SELECT` only. Data-modifying statements are refused, and the `users`,
`user_sessions`, `login_attempts` and `audit_logs` tables are out of reach entirely
— the account and audit surface is not a reporting surface.

**What you see.** The generated SQL is shown next to the answer. If you cannot see
the query, you cannot audit the answer.

### 2. AI Salesperson Assistant — order and van-load suggestions

**What it does.** Proposes what a given customer is likely to order, and what to
load onto a van, each with the reasoning that produced it.

**What the model is told.** Aggregated purchase history for the customer in
question, current stock, and campaign state.

**What it may change.** Nothing. The suggestion arrives as a proposal. The
salesperson accepts, edits or ignores it; the accepted basket is what gets written,
by the ordinary sale endpoint, under the ordinary permission checks.

### 3. AI terminal — tiered operational commands

**What it does.** A command surface with permission tiers, defaulting to
`READ_ONLY`.

**What it may change.** Depends on the tier — and **dangerous operations are
refused at every tier**, including the highest. The tier is not a way to unlock
them; there is no tier that unlocks them. See `app/ai/terminal_guard.py`.

### 4. Forecasting and anomaly detection

These are **not** AI-provider features. They are ordinary statistics computed
locally (Croston, SBA, TSB, Holt-Winters, seasonal naive), with method selection
validated by backtest. They work with every AI provider switched off, and they
report their own error (MAE / MAPE / RMSE) alongside the forecast. A forecast with
a large MAPE is still returned — with the MAPE attached.

---

## What is sent to a cloud provider

Only when you have configured one, and only for the feature you invoke.

**Sent:** the prompt — your question, the schema description, and whatever
aggregated context the feature assembles.

**Not sent:** the database, backups, credentials, the audit log, or bulk personal
records. The context is assembled per request and is aggregate in shape.

**Be aware anyway:** a question can itself contain personal data ("what has
Mehmet Yılmaz's shop bought?"), and an aggregate over a small group can identify
its members. If that matters in your setting, use LM Studio locally and leave the
cloud providers unconfigured.

If you do use a cloud provider, that is a transfer of personal data to a processor,
possibly across a border. The compliance layer has a transfer register for it. See
[PRIVACY.md](PRIVACY.md).

---

## Cost and budget

Every request records its token counts and computed cost. `VS_AI_MONTHLY_BUDGET_USD`
caps monthly cloud spend, with a warning threshold at `VS_AI_BUDGET_WARN_PCT`.
Local models do not count against the budget because they do not cost anything per
request.

The cost figures are computed from the per-1k rates configured for each provider.
They are an estimate for your own budgeting — **not a bill**. Reconcile against
your provider's own statement.

---

## Human control

- **Every AI output is a proposal.** No AI path writes a business record.
- **The reasoning is shown**, not just the conclusion. A suggestion you cannot
  interrogate is a suggestion you cannot refuse on informed grounds.
- **AI-attributed actions are marked** in the audit log (`is_ai_action`), so "the
  system did it" and "a person did it" stay distinguishable after the fact.
- **The compliance layer's authority engine** can answer, before an action, whether
  a given machine is authorised to do a given thing to a given person — and writes
  a receipt for every answer, including refusals.

### One honest caveat

The authority engine is **not currently consulted by the automated credit-limit and
risk-scoring code paths.** Those two decisions are made by ordinary business logic
and recorded in the audit log; they do not yet produce a rights receipt. The engine
is real, tested (`backend/tests/test_hsp_engine.py`) and usable through the API and
the UI, but wiring it into those decision points is roadmap, not fact.

Recorded in [docs/known-limitations.md](docs/known-limitations.md).

---

## Turning it all off

```dotenv
VS_LMSTUDIO_ENABLED=false
VS_NVIDIA_ENABLED=false
VS_CLAUDE_ENABLED=false
```

The AI screens then report every provider as unavailable, and every non-AI feature
— sales, stock, routing, invoicing, collections, reporting, statistics, forecasting,
compliance — continues to work unchanged. This state is covered by the test suite,
which runs with all three providers disabled.
