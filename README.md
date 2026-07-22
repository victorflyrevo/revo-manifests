# REVO Manifest Ingest

Upload REVO manifesto Excel workbooks, parse each sheet as a flight, and store flights + passengers in a database for long-term analytics.

## What it does

1. You upload one or more `.xlsx` manifesto files or `.csv` Base de Dados exports in the web UI  
2. Each Excel sheet (except templates like `Base de Dados` / `xxxx…`) or CSV flight group becomes a **flight**  
3. Passengers are upserted by document (or normalized name)  
4. Duplicate files/flights are skipped via content hash + flight fingerprint  
5. Dashboard/API endpoints expose monthly trends, unique/recurring passengers, routes  
6. Full boarding history exportable as CSV  

## Data model

| Table | Purpose |
|---|---|
| `upload_batches` | Each uploaded file |
| `flights` | One row per flight (deduped) |
| `passengers` | Unique people over time |
| `boardings` | Passenger × flight events |

## Local run

```bash
cd revo-manifests
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 and upload the manifesto files.

## Railway deploy

1. Push this folder to a GitHub repo  
2. In Railway: **New Project → Deploy from GitHub**  
3. Add a **PostgreSQL** plugin (Railway sets `DATABASE_URL`)  
4. Deploy — health check is `/health`  

Optional env vars:

- `DATABASE_URL` — Postgres (production) or SQLite (local)  
- `MAX_UPLOAD_MB` — default `50`  
- `PORT` — Railway injects this  

## Report API (`/api/v1`)

Read-only JSON API for external reports/dashboards. Interactive docs: `/docs`.

Auth: set env `API_KEY` and send header `X-API-Key: <key>` on every request.
If `API_KEY` is empty (local default), the API is open.

| Endpoint | Description |
|---|---|
| `GET /api/v1/summary?days=365` | KPI window (or `start_date` / `end_date`) |
| `GET /api/v1/monthly` | Boardings / flights / uniques by month |
| `GET /api/v1/routes` | Top OD routes |
| `GET /api/v1/passengers/top` | Most frequent passengers |
| `GET /api/v1/flights` | Paginated flights (`limit`, `offset`, filters) |
| `GET /api/v1/boardings` | Paginated boardings |
| `GET /api/v1/passengers?q=` | Search passengers |
| `GET /api/v1/uploads` | Recent ingest batches |
| `GET /api/v1/export/boardings.csv` | Full CSV export |

Example:

```bash
curl -H "X-API-Key: $API_KEY" \
  "https://YOUR-APP.up.railway.app/api/v1/summary?days=365"
```

Legacy aliases still work under `/api/stats/*` (no key required for now).

Upload: `POST /api/upload` (multipart field `file` — `.xlsx` / `.xlsm` / `.xls` / `.csv`).

CSV ingest accepts a flat Base de Dados table (columns such as `Data`, `Hora`, `Origem`, `Destino`, `Nome`, `Documento`, `Matrícula` — comma or semicolon) or a single manifesto-style sheet export. Rows are grouped into flights by date/time/route/aircraft.

## Security note

Set `API_KEY` on Railway for the report API. Keep the upload UI URL private or add further auth if the app is public.
