# Privacy

This document describes what personal data the software handles, where it goes,
and what the built-in compliance layer does and does not do.

**This is not legal advice.** If you run this system you are the controller of the
data in it, and the obligations are yours.

---

## The short version

- The software runs **entirely on your own infrastructure**. There is no hosted
  service, no telemetry, no analytics, no phone-home, no crash reporting.
- **Nothing leaves the installation** unless you configure a cloud AI provider
  yourself. With no key configured, the cloud providers report themselves as not
  configured and make no outbound request.
- All demo data shipped with the repository is **synthetic**, with contact details
  masked at the point of generation. No real person appears anywhere in this
  repository, its screenshots or its presentation.

---

## What personal data the system holds

Measured, not estimated: the discovery scanner
(`backend/app/compliance/scanners/discovery.py`) walks the ORM models and reports
what it finds. On a default installation with the demo dataset it reports:

| Measure | Value |
|---|---|
| Tables containing personal data | 19 |
| Personal-data fields | 81 |
| Direct identifiers | 21 |
| Location fields | 22 |
| Special-category candidates (GDPR Art. 9 / KVKK Art. 6) | 0 |

Reproduce it yourself:

```bash
cd backend
python -m app.compliance.scanners.discovery --markdown
```

or, in the running application, **Compliance → Data Inventory → Run scan**.

### Two groups of people, with different exposure

| Group | Relationship to the system | What the system does about them |
|---|---|---|
| **Salespeople** (your employees) | They use it | Location tracked while a work-day session is open; performance measured; targets evaluated |
| **Customers** (shopkeepers, retailers) | They never see it | Risk score computed; credit limit applied automatically; a sale can be refused |

The second group is the one that deserves attention: **the customer never uses the
system, but the system makes decisions about them.** A shopkeeper who is refused
goods because of a computed score generally does not know the score exists. The
compliance layer exists mainly because of that asymmetry.

### Categories held

- Identity and contact: name, trade name, contact person, phone, e-mail, address
- Commercial identifiers: tax office and number, customer code
- Location: customer coordinates, vehicle GPS traces, visit geofence results
- Behavioural: purchase history, visit history, payment timeliness, ageing buckets
- Employment: salesperson profile, hire date, commission, targets, day sessions
- Security: user accounts, sessions, IP addresses, login attempts, audit entries

---

## Where data goes

### Stays local, always
Every commercial and personal record lives in your database (SQLite by default,
PostgreSQL optionally). Backups go to `VS_BACKUP_DIR`, logs to `VS_LOG_DIR`, both
on your machine.

### Leaves only if you configure it

| Destination | When | What is sent |
|---|---|---|
| **LM Studio** (`localhost:1234`) | Enabled by default | Prompt and the aggregate context assembled for the question. Runs on your own machine — this is a local process, not a network egress. |
| **NVIDIA NIM** (`integrate.api.nvidia.com`) | Only with `VS_NVIDIA_API_KEY` set | Prompt and assembled context |
| **Anthropic** (`api.anthropic.com`) | Only with `VS_CLAUDE_ENABLED=true` and a key | Prompt and assembled context |
| **OpenStreetMap tile servers** | Whenever the map screen is open | The map tile coordinates being viewed. Standard for any web map; no customer record is transmitted, but the *area being looked at* is visible to the tile provider. |

If you use a cloud AI provider, that is a **transfer of personal data to a
processor**, potentially across a border. The compliance layer has a transfer
register for exactly this; recording it there is your job, not the software's.

See [AI_TRANSPARENCY.md](AI_TRANSPARENCY.md) for what is actually put in a prompt.

---

## What the compliance layer does

| Capability | What it actually does |
|---|---|
| **Data inventory** | Scans the ORM and records every personal-data field, its sensitivity, its identifiability and its review status. Finds fields; does not classify them for you. |
| **Notices and consent** | Versioned privacy notices with a content hash; consent records separate from notices, with withdrawal. Keeps the two apart because "informed" and "consented" are different events. |
| **Data-subject requests** | Intake, identity verification, status workflow, closure with a written evidence record. Deadlines are recorded, not computed — the software does not know which regime applies to you. |
| **Cross-border transfers** | Register of recipients, countries and mechanisms. |
| **Rule packs** | GDPR and KVKK control catalogues, loaded as **drafts**, requiring four-eyes approval before they have force. |
| **Evidence chain** | Every compliance decision writes a SHA-256-chained artefact. Altering history breaks the chain and the verifier reports where. |
| **Authority engine (rights receipts)** | Answers "is this machine authorised to do this to this person?" before the action, fail-closed, and writes a receipt for every answer — including every refusal. |

### What it does not do

- **It does not make you compliant.** It produces evidence and a review queue.
  Reading and acting on that queue is human work.
- **It does not give legal advice.** Article references in the rule packs are free
  text and are **not** validated. Every pack ships as a draft.
- **It does not decide sensitivity for you.** Fields land in `REVIEW_REQUIRED`; a
  person confirms or corrects.
- **It is not wired into the automated sales decisions.** The credit-limit refusal
  and the risk score do not currently call the authority engine. The engine is
  real and tested, but those two code paths do not consult it yet. See
  [docs/known-limitations.md](docs/known-limitations.md).
- **It does not compute your deadlines.** A request with no due date is shown as
  "period unknown", never as "not overdue". The two are different and the interface
  says so.

---

## Retention

There is **no automatic retention enforcement**. Nothing is deleted on a schedule.

This is a known gap, not an oversight: `gps_events` in particular accumulates
without limit, and location traces about employees are exactly the data that most
needs a retention policy. The compliance layer has retention-policy tables; the
enforcement job that acts on them does not exist yet. Until it does, deleting old
data is a manual operation. Recorded in
[docs/known-limitations.md](docs/known-limitations.md).

---

## Demo data

`python -m scripts.seed_demo_data` generates a complete fictional distributor.

- Every name, address and coordinate is generated.
- Every row is tagged `DEMO`.
- **Contact details are masked at the point of generation**, not afterwards:
  - phone numbers render as `+90 5XX XXX XX 42` — fewer than ten digits, cannot be
    dialled;
  - e-mail addresses use `@demo.invalid`, a domain RFC 2606 reserves so it can
    never resolve;
  - tax numbers carry a fixed `0000` prefix so they are visibly fabricated.

The masking is enforced by `backend/tests/test_demo_data_privacy.py`, which checks
both the generators and the generator's own source, so a future edit that
reintroduces a plausible number fails a test rather than appearing on a slide.

---

## Your obligations if you run this

The software will not do these for you:

1. Publish a privacy notice to the people whose data you hold, and record which
   version they saw.
2. Establish a lawful basis for each processing activity — especially employee
   location tracking and customer risk scoring.
3. Define retention periods and enforce them (see above: manual, for now).
4. Provide a route for data-subject requests and answer them within your
   applicable period.
5. Record any transfer to a cloud AI provider, with its safeguards.
6. Assess whether automated credit refusal constitutes a decision requiring human
   review under your regime, and staff that review.
7. Secure the deployment: TLS, access control, backups, patching. See
   [SECURITY.md](SECURITY.md).
