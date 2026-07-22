# Customer KPI dashboard (local)

Builds `index.html` + `revo-customer-kpis.xlsx` from the Manifests API.

Flight counts use the **Sigtrip mission cut** (`app.missions`): connected same-day legs on the same aircraft.

## Setup

```bash
cp .env.example .env.local   # if present, or create:
# MANIFESTS_API_BASE=https://…
# API_KEY=…
```

## Build

From repo root (or this folder):

```bash
python3 tools/kpi-dashboard/build_full_dashboard.py
```

Outputs are written next to the script (gitignored): `index.html`, `data.js`, `revo-customer-kpis.xlsx`.
