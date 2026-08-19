# Contributing

Thank you for looking. Please read the first section before you write any code.

---

## Licence of contributions

This project is distributed under the MIT License. By submitting a contribution,
you agree that it may be distributed under the same terms. You must have the
right to submit the work and must not include secrets or third-party code whose
licence is incompatible with MIT.

---

## Reporting a bug

Open an issue with:

- what you expected and what happened,
- the commit or version,
- Python and Node versions, operating system, and which database,
- the smallest reproduction you can manage — a request, a seed command, a sequence
  of clicks,
- the relevant log lines, **with secrets removed** (the logger redacts known
  credential shapes, but check).

For a security problem do **not** open an issue. See [SECURITY.md](SECURITY.md).

---

## Development setup

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest tests/ -q

# frontend
cd ../frontend
npm ci
npm run typecheck
npm run dev
```

Seed a working dataset with `cd backend && python -m scripts.seed_demo_data --reset`.

Remember that the administrator account is locked down until its first-run password
is changed — that is intentional. See "First sign-in" in the [README](README.md).

---

## House rules

These are the conventions the existing code follows. They are not style
preferences; each one exists because breaking it produced a bug.

### Comments explain *why*, never *what*
The code says what it does. A comment earns its place by recording the reasoning,
the alternative that was rejected, or the trap that is not obvious. Several
comments in this codebase name the exact failure that motivated the line above
them; that is the standard.

### A control that is not enforced by the server does not exist
The web client hides what a user may not do. That is usability. The API must refuse
it independently, and there must be a test proving the API refuses it. This
repository has already shipped one decorative control — a `must_change_password`
flag the client honoured and the server ignored — and one whole permission tree
that no endpoint consulted. Both are now enforced, and both have tests
(`test_bootstrap_credential.py`, `test_compliance_rbac.py`).

### Every claim in the documentation is measurable
The figures in the README and on the presentation slides are computed from the
source at build time (`tanitim_uret.py::_olcum`), not typed in. If you add a claim,
add the measurement — or do not add the claim.

### Fail closed
Compliance and authorisation code refuses on error, on absence and on ambiguity.
`UNKNOWN` is never promoted to "allowed". If a decision cannot be recorded, the
decision cannot be "allow" — an authority exercised without evidence was not
exercised.

### Errors carry i18n keys, not sentences
Raise `ValidationError("customer.credit_limit_exceeded", params={...})`. The API
layer renders it in the caller's language. Both catalogues
(`backend/app/locales/{tr,en}.json`) must gain the key — a test asserts they stay
symmetric.

### Never weaken a test to make it pass
If a test fails, either the code is wrong or the requirement changed. If the
requirement changed, say so in the test's docstring and strengthen the assertion
rather than deleting it.

### Money is `Decimal`
`app.core.utils.money()`, four decimal places, half-up rounding. Never `float`.
Banker's rounding is wrong for invoices and a test enforces that.

### Turkish text needs Turkish-aware helpers
`tr_upper` / `tr_lower` / `slugify` / `normalize_search` from `app.core.utils`.
`"istanbul".upper()` is `"ISTANBUL"` in Python and `"İSTANBUL"` in Turkish, and
SQLite's `LOWER()` is ASCII-only — which is why the search-key columns exist.

---

## Tests

Every behavioural change needs a test. New tests go in `backend/tests/` and should
read as a statement of the property being fixed:

```python
def test_remote_login_is_refused_with_the_correct_password(self):
    """The password is right; the network is wrong. That is still a refusal."""
```

Useful entry points:

| File | Property under guard |
|---|---|
| `test_bootstrap_credential.py` | First-run credential: no default password, forced change, local-only, one-way flags |
| `test_compliance_rbac.py` | Every compliance route enforces its own declared permission |
| `test_ui_api_contract.py` | Every URL the client calls exists; every permission it names is real |
| `test_api_key_policy.py` | No credential in the tree; masking; graceful degradation without a key |
| `test_demo_data_privacy.py` | Demo contact details are masked at generation |
| `test_hsp_engine.py` | Default deny, expiry, revocation, human approval, receipt chain integrity |

Run everything before you push:

```bash
cd backend  && python -m pytest tests/ -q && ruff check app scripts tests
cd frontend && npm run typecheck && npm run build
```

---

## Commits and pull requests

- One logical change per commit; a message that says why.
- Keep the diff readable. Formatting churn in the same commit as a behaviour change
  makes review impossible.
- Update the documentation in the same change, not afterwards. If you alter a
  measured figure, re-run the measurement.
- Add a `CHANGELOG.md` entry under *Unreleased*.

---

## Language

Code, identifiers and commit messages are English. The user interface is Turkish
and English, in equal standing. Some comments and docstrings in the compliance
module are Turkish because they discuss Turkish law and the precision matters more
than uniformity; that is fine — follow whichever language the file already uses.
