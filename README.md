# REVO Manifest Ingest

Upload REVO manifesto Excel workbooks, parse each sheet as a flight, and store flights + passengers in a database for long-term analytics.

## What it does

1. You upload one or more `.xlsx` manifesto files in the web UI  
2. Each sheet (except templates like `Base de Dados` / `xxxx…`) becomes a **flight**  
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

## API for a future dashboard

- `GET /api/stats/summary?days=365`  
- `GET /api/stats/monthly`  
- `GET /api/stats/top-routes?days=365`  
- `GET /api/stats/top-passengers?days=365`  
- `GET /api/exports/boardings.csv`  
- `POST /api/upload` — multipart field `file`  

## Security note

This MVP has **no auth**. Before sharing publicly, add Railway private networking, basic auth, or SSO. For internal use, keep the Railway URL private or protect with Railway’s auth / Cloudflare Access.
