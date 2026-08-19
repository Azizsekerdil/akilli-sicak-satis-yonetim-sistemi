# User Guide

**Smart Van Sales Management System — v1.0.0**

This guide covers everything from installation to the daily field routine.
The application also contains an interactive 14-lesson course under
**System → Training Centre** that walks through the same material on-screen.

---

## Contents

1. [Installation](#1-installation)
2. [First run](#2-first-run)
3. [Users and roles](#3-users-and-roles)
4. [Products](#4-products)
5. [Warehouses and stock](#5-warehouses-and-stock)
6. [Customers](#6-customers)
7. [Salespeople and vehicles](#7-salespeople-and-vehicles)
8. [Route planning](#8-route-planning)
9. [Van loading](#9-van-loading)
10. [Hot sale](#10-hot-sale)
11. [Collections](#11-collections)
12. [Returns](#12-returns)
13. [Day-end reconciliation](#13-day-end-reconciliation)
14. [Campaigns](#14-campaigns)
15. [Reports and statistics](#15-reports-and-statistics)
16. [Artificial intelligence](#16-artificial-intelligence)
17. [Backup and restore](#17-backup-and-restore)
18. [System settings](#18-system-settings)
19. [Troubleshooting](#19-troubleshooting)

---

## 1. Installation

### Requirements

| Component | Version | Required |
|---|---|---|
| Windows | 10 / 11 | Yes |
| Python | 3.11+ | Yes |
| Node.js | 20+ | For the web client |
| PostgreSQL | 14+ | **No** — SQLite is the default |
| Redis | — | **No** — optional |
| Docker | — | **No** |

### Steps

```powershell
cd <install-directory>
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithDemoData
.\start.bat
```

Setup creates the virtual environment, installs backend and frontend packages,
writes a `.env` with a freshly generated secret key, creates the database schema,
seeds reference data and — with `-WithDemoData` — generates a realistic sample
dataset.

Two windows open (backend and frontend) and the browser goes to
`http://localhost:5173`.

---

## 2. First run

From the host computer, sign in once as **`admin` / `admin`**. This temporary
bootstrap credential must be changed immediately and cannot be used remotely.

You will be asked to change it at first sign-in
(**My Profile → Change Password**).

The **TR / EN** button in the top bar switches the interface, error messages and
reports instantly; your choice is saved to your account.

Enter company details under **System → Settings → General** — they appear on
invoices and reports.

---

## 3. Users and roles

Create users under **System → Users → New**. Passwords need at least 8
characters with upper case, lower case and a digit.

| Role | Data scope | Typical use |
|---|---|---|
| System Administrator | All | Setup, users, backups |
| Company Owner | All | Full visibility |
| General Manager | All | Dashboard, all reports |
| Sales Manager | All | Sales, field, CRM, campaigns |
| Regional Sales Manager | Own region | Regional operations |
| Field Sales Supervisor | Own team | Supervision, count approval |
| **Salesperson** | Own records | **Hot sale, collections, visits** |
| Driver | Own records | Route and vehicle visibility |
| Merchandiser | Own records | Visits, shelf work |
| Warehouse Manager | All | Stock, transfers, counts |
| Warehouse Staff | Own records | Loading, count entry |
| Logistics Staff | All | Routes, vehicles, transfers |
| Accounting | All | Invoices, collections, ledger |
| Collection Staff | All | Collections and risk |
| Marketing / Trade Marketing | All | Campaigns, price lists |
| Sales Analyst | All | Analytics and forecasting |
| AI Manager | All | AI providers and budget |
| Auditor | All (read-only) | Audit and compliance |

Per-user exceptions are set under **System → Users → (user) → Permissions**.

> Guard rail: you cannot assign a role above your own, and you cannot grant a
> permission you do not hold yourself. The system refuses both.

---

## 4. Products

**Stock → Products → New**

Mandatory: SKU, name, **base unit** (how stock is held — usually PIECE),
**sales unit** (what the field uses — usually CASE) and **units per case**.

> Stock is always held in the **base unit**. The field enters cases, the system
> converts. That way changing a case definition later cannot corrupt history.

Set **shelf life (days)** for food items — the system adds it to the production
date when creating a lot and uses the result for **FEFO** picking. **Minimum
remaining shelf life** stops short-dated stock being loaded onto a van.

A product can carry several barcodes (piece and case); scanning selects the
right unit automatically.

---

## 5. Warehouses and stock

| Type | Use |
|---|---|
| Central | Main stock from the factory |
| Regional | Regional distribution point |
| Transit | Temporary transfer point |
| **Vehicle** | Each sales van's own stock |
| Quarantine | Damaged or blocked goods |

> A van *is* a warehouse, so lot tracking, FEFO, counting and valuation all work
> on it unchanged.

**Goods receipt** requires lot number, production date and expiry date for
lot-tracked products.

**Transfers** run Draft → Ship → Receive. Received quantities may differ from
shipped ones; the difference is reported.

**Counts**: the system pre-fills current quantities, you enter counted ones, and
**Approve** posts an adjustment movement for every variance plus an audit entry.
An approved count cannot be undone — correct it with another count.

**Stock → Lots & Expiry** lists near-expiry and expired stock. The threshold is
**Settings → Stock → Expiry Warning Days** (default 30).

---

## 6. Customers

**CRM → Customers → New**

| Section | Field | Why it matters |
|---|---|---|
| Identity | Legal name, trade name, tax no | Printed on invoices |
| Location | **GPS coordinates** | Route optimisation and visit verification |
| Visits | Visit days, frequency, service time | Route generation |
| Commercial | Credit limit, risk limit, terms | Enforced during sale |
| Pricing | Price list | Price applied in the field |

> **Without GPS coordinates** a customer cannot take part in route optimisation
> and visits cannot be verified. On a mobile device the "use my location" button
> captures the coordinates while standing at the shop.

A credit limit of **0** means unlimited. Above zero, a sale is refused when
balance + order value would exceed it.

The **Current Account** tab shows every debit and credit with a running balance;
**Statement** produces a PDF for a chosen date range.

---

## 7. Salespeople and vehicles

Creating a vehicle under **Field → Vehicles → New** **automatically creates its
warehouse** — no separate setup needed. Volume and weight capacity are enforced
during van loading.

Link each salesperson (**Field → Salespeople**) to a **user account** so the
person in the field signs in as themselves and sees only their own data. The
**max discount** field caps what they can give away.

---

## 8. Route planning

Create weekday **template routes** and add customers. **Generate Daily Routes**
creates today's routes from customers' visit days.

Open a route and press **Optimize**. The solver honours customer coordinates,
vehicle capacity, service times and opening hours, then rewrites the stop order
and reports total distance and duration — including which solver ran.

> If OR-Tools is installed the exact solver is used; otherwise the built-in
> Clarke-Wright + 2-opt solver runs. **Optimisation works either way.**

The **Plan vs Actual** tab shows completed, skipped and delayed stops with the
planned/actual kilometre and time difference.

---

## 9. Van loading

**Stock → Van Loading**

Choose salesperson, vehicle, date and source warehouse, then press **Get AI
suggestion**. The system considers past consumption of the customers on today's
route, day-of-week effects, the last four weeks' trend, active campaigns, stock
already on the van, vehicle capacity, depot availability and remaining shelf
life.

Every line shows its **reason** and a confidence level. Adjust quantities, then
**Save** → **Post**. Posting moves stock out of the depot and into the van,
recording movements on both sides.

---

## 10. Hot sale

**Sales → Hot Sale** — the most used screen in the system.

1. **Pick the customer.** Balance, credit limit, risk and recent purchases are
   shown.
2. **Review AI suggestions.** Based on average consumption and days since the
   last purchase, the system estimates which products have likely run out and
   how much to suggest — with its reasoning.
3. **Build the basket** from van stock. Enter quantity and unit; apply a line
   discount if you are allowed to.
4. **Campaigns apply automatically.** Free goods appear as their own line with
   an explanation.
5. **Take payment** — cash, card, transfer, cheque or open account.
6. **Complete Sale.**

Behind the scenes, one database transaction creates: order → delivery → FEFO
stock issue (recording exactly which lot left) → invoice → ledger entry →
payment → audit record. If any step fails, **none of it is written** — there is
no such thing as a half-finished sale.

**Offline**: the basket is held on the device with a unique id and submitted
when the connection returns. Submitting the same sale twice never creates a
duplicate.

---

## 11. Collections

**Sales → Collections → New**

| Method | Behaviour |
|---|---|
| Cash / Card / Transfer | Reduces the balance immediately |
| **Cheque / Promissory note** | Recorded as **pending** — does *not* reduce the balance |
| Open account | Remains as debt |

Mark a cheque **Cleared** when it is honoured or **Bounced** when it is not.
Only clearing reduces the balance.

Payments are allocated to the **oldest open invoice first**; you can override
the allocation manually.

---

## 12. Returns

**Sales → Returns → New**. Each line carries its own **reason** and
**disposition**:

| Disposition | Result |
|---|---|
| Resaleable | Goes back into stock |
| Scrap | Posted as wastage, not returned to stock |
| Quarantine | Enters stock in quarantine status, cannot be sold |

Posting a return optionally raises a credit note and credits the ledger.

---

## 13. Day-end reconciliation

**Field → Day Sessions**

Open the day in the morning (vehicle + route, optionally odometer). At the end
of the day enter the **physical van count** and the **declared cash**, then
**Close Day**.

```
theoretical = opening + loaded + reloaded − sold + returned − wastage
variance    = theoretical − counted
```

A non-zero variance flags the session, writes adjustment movements, records an
audit entry and notifies the manager. Cash is reconciled the same way.

> This is the screen that settles the "three cases were missing tonight"
> argument. Every figure comes from the movement ledger, not from a
> hand-maintained counter.

---

## 14. Campaigns

**Marketing → Campaigns → New**

| Type | Example |
|---|---|
| Buy X get Y | Buy 10 cases, get 1 free |
| Quantity discount | 5% over 5 cases |
| Value discount | 3% over ₺20,000 |
| Basket mix | Extra discount for 3 different products |
| Fixed price | Special price for a given customer |
| Percent / amount discount | General reduction |

Scope can be everyone, or a specific customer, customer type, channel, region,
route, salesperson, product, category or brand.

**Preview** prices a hypothetical basket so you can see what a campaign would
give before saving it. The **ROI** tab shows discount given, free-goods cost,
revenue and net effect.

---

## 15. Reports and statistics

**Analytics → Reports** — 21 built-in reports covering sales by period,
salesperson, customer, SKU, brand, category, region and route performance,
collections, receivable risk, stock, van stock, expiry, wastage, returns,
campaigns, profitability and target achievement. Each exports to **PDF**,
**Excel** and **CSV**.

> Excel and CSV are written as UTF-8 with BOM so Turkish characters open
> correctly in Excel without any import step.

**Analytics → Statistics** provides mean, median, mode, standard deviation,
variance, quartiles and percentiles; time-series trend, moving average and
WoW/MoM/YoY change; a correlation matrix and regression analysis.

**Analytics → Forecasts** produces forward demand for a product, customer or
salesperson. The system classifies the series (smooth / intermittent / lumpy)
and picks the method accordingly, validating the choice by back-testing. The
screen shows the method used, its error and the confidence interval.

---

## 16. Artificial intelligence

### Providers

**AI → AI Providers**

| Provider | Setup |
|---|---|
| **LM Studio** | Local and free. Start the server in LM Studio; address `http://localhost:1234/v1` |
| **NVIDIA** | Add `VS_NVIDIA_API_KEY` to `.env` |
| **Claude** | Add `VS_CLAUDE_API_KEY` and set `VS_CLAUDE_ENABLED=true` |

**Test Connection** makes a real call and reports latency.

> API keys are **never stored in the database** — only a "key is configured"
> flag. Keys are masked on screen and stripped from logs.

### AI Sales Manager

**AI → AI Sales Manager** answers plain-language questions:

- "Show the 10 best-selling salespeople today"
- "Find customers whose sales dropped in the last 30 days"
- "Show customers with high collection risk"
- "List the 20 most profitable products"
- "Which customers did we lose in the last 90 days?"

The question is turned into a **read-only** query, executed, and the result
explained. The **Source data** panel shows exactly what was run.

> Safety: only a single read query is ever executed. No data-modifying command
> is accepted, and the user, session and audit tables are off limits.

### AI Field Assistant

Produces per-customer order suggestions and van load suggestions, each with its
reasoning.

### Tokens and cost

**AI → Tokens & Cost** shows daily and monthly usage, estimated cost and budget
consumption. When the budget is exhausted, **paid** providers stop while the
**local model keeps working**.

---

## 17. Backup and restore

**System → Backup**

**Back Up Now** takes a consistent copy of the database, stamps it with a
SHA-256 checksum and compresses it into `backups/`. Schedule automatic backups
under **Settings → Backup**.

**Verify** re-checks a backup's integrity. An unverified backup is a hope, not a
recovery plan — verify regularly.

**Restore** asks for two-step confirmation, verifies the archive first, then
takes a **safety backup of the current database** before overwriting. That means
even an unwanted restore can itself be undone.

---

## 18. System settings

**System → Settings**, grouped by category: General, Sales, Stock, Route, AI and
Backup.

**System → System Health** checks nine components — backend, database, Redis,
LM Studio, NVIDIA, Claude, disk, backup and queue — each reporting
**OK / WARNING / ERROR / UNKNOWN**.

**System → Audit Log** lists every critical operation. **Verify Chain** checks
whether any historical entry has been altered; if so, the system reports exactly
where the chain breaks.

---

## 19. Troubleshooting

**System will not start** — check `logs\error.log`, or run:

```powershell
.\.venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'backend');from app.main import app;print('OK')"
```

**Cannot sign in** — an account locks for 15 minutes after 5 failed attempts.
Another administrator can clear it via **System → Users → (user) → Status:
Active**.

**LM Studio will not connect** — is LM Studio running, was
**Developer → Start Server** pressed, and is a model loaded? Then use **Test
Connection**.

**Broken Turkish characters** — exports are UTF-8 with BOM. If a tool still
struggles, import with encoding set explicitly to **UTF-8**.

**Reload demo data**:

```powershell
cd <install-directory>\backend
..\.venv\Scripts\python.exe -m scripts.seed_demo_data --reset
```

> `--reset` deletes existing demo data. **Do not run it against real data** —
> take a backup first.

**Move to PostgreSQL**:

1. Install PostgreSQL and create an empty database
2. `.venv\Scripts\python.exe -m pip install "psycopg[binary]"`
3. Set `VS_DATABASE_URL=postgresql+psycopg://YOUR_DB_USER:YOUR_DB_PASSWORD@localhost:5432/van_sales`
4. `cd backend && ..\.venv\Scripts\python.exe -m alembic upgrade head`

**Logs**

| File | Contents |
|---|---|
| `logs\application.log` | General activity |
| `logs\error.log` | Errors only |
| `logs\ai.log` | AI calls |
| `logs\security.log` | Sign-in, authorisation, audit |

> API keys, passwords and tokens are **never** written to logs — they are
> redacted automatically.

---

## Further reading

See `ARCHITECTURE.md` for design decisions, `CHANGELOG.md` for release history
and `THIRD_PARTY_NOTICES.md` for the dependency licence ledger.
