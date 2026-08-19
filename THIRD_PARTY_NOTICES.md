# Third-party notices

Every third-party component this project depends on, with its licence.

Generated from the dependency manifests and the installed distribution metadata,
and cross-checked against the SBOMs shipped alongside this file:

- `sbom.spdx.json` — SPDX 2.3
- `sbom.cdx.json` — CycloneDX 1.7

Regenerate the inventories with:

```bash
syft dir:. -o spdx-json=sbom.spdx.json -o cyclonedx-json=sbom.cdx.json
```

> This file lists licences as declared by each project's own metadata. It is a
> record, not a legal opinion.

---

## Summary

| Ecosystem | Packages | Copyleft (GPL / AGPL / LGPL) | Non-OSI or field-of-use restricted |
|---|---|---|---|
| Python (runtime + dev) | 21 direct | **0** | **0** |
| npm (full dependency tree) | 514 | **0** | **0** |

Licence distribution across the full npm tree: MIT 452 · ISC 31 · BlueOak-1.0.0 8 ·
Apache-2.0 8 · BSD-3-Clause 7 · BSD-2-Clause 5 · CC-BY-4.0 1 · (MIT OR CC0-1.0) 1 ·
MIT AND ISC 1. No package reports an unknown licence.

---

## One dependency was removed for licence reasons

**`react-leaflet` 4.2.1 and `@react-leaflet/core` 2.1.0 — Hippocratic Licence 2.1.**

The Hippocratic Licence is **not OSI-approved** and carries field-of-use
restrictions: it forbids use in specified categories of activity. Those terms do
not compose with a permissive release, and — more practically — they impose an
obligation on every downstream user that a permissive licence promises they will
not have.

Both packages were removed. The map screen
(`frontend/src/pages/field/MapView.tsx`) was rewritten against **Leaflet itself**,
which has always been **BSD-2-Clause**. The components that were in use
(`MapContainer`, `TileLayer`, `Marker`, `CircleMarker`, `Polyline`, `Popup`) are
thin wrappers over Leaflet's imperative API, so the rewrite is behaviour-preserving:
the map still renders vehicles, customers, warehouses, route polylines, layer
toggles and popups, and it still uses `L.divIcon` markers built from inline SVG so
no marker image assets are needed.

`grep -r react-leaflet` over this repository now returns only the explanations
(this file, the header comment in `MapView.tsx`, and the changelog entry).

---

## Map data

The map screen renders **OpenStreetMap** tiles.

- Data © OpenStreetMap contributors, licensed under the
  [Open Database Licence (ODbL) 1.0](https://www.openstreetmap.org/copyright).
- The attribution control in `MapView.tsx` is a **licence condition**, not
  decoration. Do not remove it.
- The public tile servers are provided on a best-effort basis under the
  [OSMF tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
  A production deployment with real traffic should point `TileLayer` at its own
  tile server or a commercial provider.

---

## Python — runtime

| Package | Version | Licence |
|---|---|---|
| fastapi | 0.141.1 | MIT |
| starlette | 1.6.0 | BSD-3-Clause |
| uvicorn[standard] | 0.52.4 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| SQLAlchemy | 2.0.52 | MIT |
| alembic | 1.18.2 | MIT |
| pydantic | 2.13.4 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| bcrypt | 5.0.0 | Apache-2.0 |
| PyJWT | 2.13.0 | MIT |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| numpy | 2.2.1 | BSD-3-Clause |
| pandas | 2.2.3 | BSD-3-Clause |
| openpyxl | 3.1.5 | MIT |
| reportlab | 4.2.5 | BSD-3-Clause |

### Optional, not installed by default

| Package | Licence | Note |
|---|---|---|
| ortools | Apache-2.0 | Exact VRP solver. Absent, the built-in Clarke-Wright + 2-opt solver is used. |
| psycopg[binary] | **LGPL-3.0** | PostgreSQL driver. Deliberately **not** a default dependency — the only copyleft component anywhere near this project, and it is opt-in, unmodified, and dynamically linked. Installing it is your decision and your licence obligation. |
| redis | MIT | Cache / queue. Absent, an in-process implementation is used. |

## Python — development only

Not shipped to users; needed to run the tests and the linters.

| Package | Version | Licence |
|---|---|---|
| pytest | 9.0.3 | MIT |
| pytest-cov | 7.1.0 | MIT |
| pytest-asyncio | 1.3.0 | Apache-2.0 |
| ruff | 0.15.5 | MIT |
| pip-audit | 2.9.0 | Apache-2.0 |

---

## npm — direct dependencies

| Package | Version | Licence |
|---|---|---|
| @tanstack/react-query | 5.101.4 | MIT |
| clsx | 2.1.1 | MIT |
| date-fns | 4.4.0 | MIT |
| i18next | 24.2.3 | MIT |
| **leaflet** | **1.9.4** | **BSD-2-Clause** |
| lucide-react | 0.469.0 | ISC |
| react | 18.3.1 | MIT |
| react-dom | 18.3.1 | MIT |
| react-i18next | 15.7.4 | MIT |
| react-router-dom | 7.18.2 | MIT |
| recharts | 2.15.4 | MIT |

### npm — development only

| Package | Version | Licence |
|---|---|---|
| @types/leaflet | 1.9.22 | MIT |
| @types/node | 26.2.0 | MIT |
| @types/react | 18.3.31 | MIT |
| @types/react-dom | 18.3.7 | MIT |
| @vitejs/plugin-react | 4.7.0 | MIT |
| autoprefixer | 10.5.4 | MIT |
| postcss | 8.5.26 | MIT |
| tailwindcss | 3.4.19 | MIT |
| typescript | 5.9.3 | Apache-2.0 |
| vite | 6.4.3 | MIT |
| vite-plugin-pwa | 0.21.2 | MIT |

The transitive tree (514 packages) is enumerated in the SBOMs.

---

## Fonts in the presentation

The deck generator (`tanitim_uret.py`) specifies **DejaVu Sans** — a Bitstream Vera
derivative under a permissive, freely redistributable licence, and one that carries
the full Turkish character set.

**What the shipped PDFs actually embed is not DejaVu Sans.** PowerPoint can only
embed fonts that are installed on the build machine; DejaVu Sans was not installed
on the machine that produced `docs/presentation/*.pdf`, so PowerPoint silently
substituted and the PDFs embed **subsets of Verdana and Arial** instead:

```
BCDEEE+Verdana-Bold, BCDFEE+Verdana, BCDGEE+Verdana, BCDHEE+Verdana-Bold, BCDIEE+ArialMT
```

Verdana and Arial are proprietary Microsoft/Monotype typefaces. Subset embedding in
a PDF for viewing and printing is the ordinary licensed use of a font installed on
the authoring machine, so the shipped PDFs are usable — but they are not what the
source asks for.

To reproduce the deck with the intended libre font, install DejaVu Sans on the
build machine before running `python tanitim_uret.py`, then confirm with:

```python
import fitz
doc = fitz.open("docs/presentation/Van_Sales_Tanitim_PUBLIC.pdf")
print({f[3] for page in doc for f in page.get_fonts(full=True)})
```

This mismatch is also recorded in [docs/known-limitations.md](docs/known-limitations.md).

---

## Tooling used to produce this release

Not dependencies — recorded so the verification is reproducible.

| Tool | Purpose |
|---|---|
| syft | SBOM generation (SPDX + CycloneDX) |
| grype, trivy, osv-scanner, pip-audit, npm audit | Dependency vulnerability scanning |
| gitleaks, detect-secrets | Secret scanning |
| semgrep, bandit, ruff | Static analysis and linting |
| pypdf, PyMuPDF, RapidOCR | Verification of the generated PDFs, including OCR of every rendered page |
| Playwright | Screenshot capture from the running application |
| python-pptx + Microsoft PowerPoint | Deck generation and PDF/PNG export |
