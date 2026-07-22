from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api_report import router as report_router
from app.auth import (
    COOKIE_NAME,
    identity_enabled,
    optional_identity,
    require_identity,
    require_uploader,
)
from app.config import settings
from app.db import SessionLocal, get_db, init_db
from app.ingest import ingest_workbook
from app.models import Boarding, Flight, Passenger, UploadBatch

BASE_DIR = Path(__file__).resolve().parent
BR_TZ = ZoneInfo("America/Sao_Paulo")


def _local_upload_dt(uploaded_at: datetime) -> datetime:
    dt = uploaded_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BR_TZ)


def serialize_upload(batch: UploadBatch) -> dict:
    local = _local_upload_dt(batch.uploaded_at)
    return {
        "id": batch.id,
        "filename": batch.filename,
        "status": batch.status,
        "day": local.strftime("%d/%m/%Y"),
        "time": local.strftime("%H:%M:%S"),
        "when_label": local.strftime("%d/%m/%Y às %H:%M"),
        "flights_found": batch.flights_found,
        "flights_inserted": batch.flights_inserted,
        "flights_skipped": batch.flights_skipped,
        "boardings_inserted": batch.boardings_inserted,
        "notes": batch.notes,
        "uploaded_at": local.isoformat(),
    }


def recent_uploads(db: Session, limit: int = 30) -> list[UploadBatch]:
    return list(
        db.scalars(
            select(UploadBatch).order_by(UploadBatch.uploaded_at.desc()).limit(limit)
        ).all()
    )

# Large workbooks (400+ sheets) need a dedicated worker, no sheet count cap
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ingest")

app = FastAPI(
    title=settings.app_title,
    description="Upload REVO manifests and expose a read API for external reports.",
)
_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(report_router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.on_event("startup")
def on_startup() -> None:
    Path("data").mkdir(exist_ok=True)
    init_db()


def _ingest_in_thread(data: bytes, filename: str) -> UploadBatch:
    db = SessionLocal()
    try:
        return ingest_workbook(db, data, filename)
    finally:
        db.close()


def _forgot_password_url() -> str:
    issuer = (settings.identity_issuer_url or "").rstrip("/")
    return f"{issuer}/forgot-password" if issuer else "#"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    claims = optional_identity(
        request,
        request.headers.get("authorization"),
        request.cookies.get(COOKIE_NAME),
    )
    if claims is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "title": settings.app_title,
            "error": None,
            "forgot_password_url": _forgot_password_url(),
        },
    )


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    issuer = (settings.identity_issuer_url or "").rstrip("/")
    client_id = (settings.identity_client_id or "").strip() or "revo-manifests"
    if not issuer:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "title": settings.app_title,
                "error": "IDENTITY_ISSUER_URL não configurada",
                "forgot_password_url": _forgot_password_url(),
            },
            status_code=503,
        )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.post(
                f"{issuer}/v1/auth/login",
                json={
                    "username": username.strip(),
                    "password": password,
                    "client_id": client_id,
                },
            )
        data = res.json() if res.content else {}
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "title": settings.app_title,
                "error": f"Falha ao contactar Identity: {exc}",
                "forgot_password_url": _forgot_password_url(),
            },
            status_code=502,
        )

    if res.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, list):
            detail = "; ".join(
                str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail
            )
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "title": settings.app_title,
                "error": detail or "Usuário ou senha inválidos",
                "forgot_password_url": _forgot_password_url(),
            },
            status_code=401,
        )

    access = data.get("access_token")
    if not access:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "title": settings.app_title,
                "error": "Resposta de login sem access_token",
                "forgot_password_url": _forgot_password_url(),
            },
            status_code=502,
        )

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        key=COOKIE_NAME,
        value=access,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=int(data.get("expires_in") or 600),
        path="/",
    )
    refresh = data.get("refresh_token")
    if refresh:
        resp.set_cookie(
            key="revo_refresh",
            value=refresh,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=60 * 60 * 24 * 30,
            path="/",
        )
    return resp


@app.post("/logout")
@app.get("/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    resp.delete_cookie("revo_refresh", path="/")
    return resp


def _identity_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE_NAME)


@app.get("/revo/apps")
async def revo_apps(request: Request) -> JSONResponse:
    issuer = (settings.identity_issuer_url or "").rstrip("/")
    token = _identity_bearer(request)
    if not issuer or not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"{issuer}/v1/auth/my-apps",
            headers={"Authorization": f"Bearer {token}"},
        )
    data = res.json() if res.content else {}
    return JSONResponse(data, status_code=res.status_code)


@app.get("/revo/launch", response_model=None)
async def revo_launch(request: Request, client_id: str = ""):
    issuer = (settings.identity_issuer_url or "").rstrip("/")
    token = _identity_bearer(request)
    if not issuer or not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not client_id.strip():
        raise HTTPException(status_code=400, detail="client_id required")
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{issuer}/v1/auth/sso-ticket",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"target_client_id": client_id.strip()},
        )
    data = res.json() if res.content else {}
    if res.status_code >= 400:
        return JSONResponse(data, status_code=res.status_code)
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return JSONResponse(
            {
                "launch_url": data.get("launch_url"),
                "target_client_id": data.get("target_client_id"),
            }
        )
    return RedirectResponse(url=data["launch_url"], status_code=302)


@app.get("/auth/sso")
async def auth_sso(request: Request, ticket: str = "") -> RedirectResponse:
    issuer = (settings.identity_issuer_url or "").rstrip("/")
    client_id = (settings.identity_client_id or "").strip() or "revo-manifests"
    if not issuer or not ticket:
        return RedirectResponse(url="/login", status_code=303)
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            f"{issuer}/v1/auth/sso-exchange",
            json={"ticket": ticket, "client_id": client_id},
        )
    data = res.json() if res.content else {}
    if res.status_code >= 400 or not data.get("access_token"):
        return RedirectResponse(url="/login", status_code=303)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        key=COOKIE_NAME,
        value=data["access_token"],
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=int(data.get("expires_in") or 600),
        path="/",
    )
    if data.get("refresh_token"):
        resp.set_cookie(
            key="revo_refresh",
            value=data["refresh_token"],
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=60 * 60 * 24 * 30,
            path="/",
        )
    return resp


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    db: Session = Depends(get_db),
    claims: dict = Depends(optional_identity),
):
    if identity_enabled() and claims is None:
        return RedirectResponse(url="/login", status_code=303)
    flights = db.scalar(select(func.count()).select_from(Flight)) or 0
    boardings = db.scalar(select(func.count()).select_from(Boarding)) or 0
    passengers = db.scalar(select(func.count()).select_from(Passenger)) or 0
    uploads = [serialize_upload(u) for u in recent_uploads(db, limit=30)]
    issuer = (settings.identity_issuer_url or "").rstrip("/")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "flights": flights,
            "boardings": boardings,
            "passengers": passengers,
            "uploads": uploads,
            "title": settings.app_title,
            "identity_url": issuer,
            "identity_client_id": (settings.identity_client_id or "revo-manifests").strip(),
            "show_revo_bar": bool(issuer and claims is not None),
        },
    )


@app.get("/api/uploads/recent")
def api_recent_uploads(
    db: Session = Depends(get_db),
    limit: int = 30,
    _claims: dict = Depends(require_identity),
) -> JSONResponse:
    limit = max(1, min(limit, 100))
    return JSONResponse(
        {"uploads": [serialize_upload(u) for u in recent_uploads(db, limit=limit)]}
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True, "identity": identity_enabled()}


@app.post("/api/upload")
async def upload_manifest(
    file: UploadFile = File(...),
    _claims: dict = Depends(require_uploader),
) -> JSONResponse:
    """Accept one workbook or CSV base and process every flight (no sheet-count limit)."""
    if not file.filename:
        raise HTTPException(400, "Missing filename")
    lower = file.filename.lower()
    if not lower.endswith((".xlsx", ".xlsm", ".xls", ".ods", ".csv")):
        raise HTTPException(
            400,
            "Only Excel (.xlsx/.xlsm/.xls), ODS (.ods), or CSV (.csv) files are supported",
        )

    data = await file.read()
    if settings.max_upload_mb > 0:
        max_bytes = settings.max_upload_mb * 1024 * 1024
        if len(data) > max_bytes:
            raise HTTPException(400, f"File exceeds {settings.max_upload_mb} MB")

    loop = asyncio.get_running_loop()
    try:
        batch = await loop.run_in_executor(
            _executor, _ingest_in_thread, data, file.filename
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Failed to parse file: {exc}") from exc

    return JSONResponse(
        {
            "upload_id": batch.id,
            "filename": batch.filename,
            "status": batch.status,
            "flights_found": batch.flights_found,
            "flights_inserted": batch.flights_inserted,
            "flights_skipped": batch.flights_skipped,
            "boardings_inserted": batch.boardings_inserted,
            "notes": batch.notes,
            "already_ingested": batch.flights_inserted == 0
            and "already ingested" in (batch.notes or "").lower(),
        }
    )


@app.post("/api/upload/batch")
async def upload_manifests_batch(
    files: list[UploadFile] = File(...),
    _claims: dict = Depends(require_uploader),
) -> JSONResponse:
    """Upload many workbooks/CSVs at once; each is fully processed."""
    if not files:
        raise HTTPException(400, "No files provided")

    results = []
    for file in files:
        if not file.filename or not file.filename.lower().endswith(
            (".xlsx", ".xlsm", ".xls", ".ods", ".csv")
        ):
            results.append(
                {
                    "filename": file.filename or "(unknown)",
                    "status": "error",
                    "error": "Only Excel (.xlsx/.xlsm/.xls), ODS (.ods), or CSV (.csv) files are supported",
                }
            )
            continue

        data = await file.read()
        if settings.max_upload_mb > 0:
            max_bytes = settings.max_upload_mb * 1024 * 1024
            if len(data) > max_bytes:
                results.append(
                    {
                        "filename": file.filename,
                        "status": "error",
                        "error": f"File exceeds {settings.max_upload_mb} MB",
                    }
                )
                continue

        loop = asyncio.get_running_loop()
        try:
            batch = await loop.run_in_executor(
                _executor, _ingest_in_thread, data, file.filename
            )
            results.append(
                {
                    "filename": batch.filename,
                    "status": batch.status,
                    "flights_found": batch.flights_found,
                    "flights_inserted": batch.flights_inserted,
                    "flights_skipped": batch.flights_skipped,
                    "boardings_inserted": batch.boardings_inserted,
                    "notes": batch.notes,
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "filename": file.filename,
                    "status": "error",
                    "error": str(exc),
                }
            )

    ok = sum(1 for r in results if r.get("status") != "error")
    return JSONResponse(
        {
            "files_total": len(results),
            "files_ok": ok,
            "files_error": len(results) - ok,
            "results": results,
        }
    )


@app.get("/api/stats/summary")
def stats_summary(
    days: int = 365,
    db: Session = Depends(get_db),
) -> dict:
    end = date.today()
    start = end - timedelta(days=days)

    flights = db.scalar(
        select(func.count())
        .select_from(Flight)
        .where(Flight.flight_date >= start, Flight.flight_date <= end)
    ) or 0
    boardings = db.scalar(
        select(func.count())
        .select_from(Boarding)
        .where(Boarding.flight_date >= start, Boarding.flight_date <= end)
    ) or 0

    # Unique passengers with ≥1 boarding in window
    unique = db.scalar(
        select(func.count(func.distinct(Boarding.passenger_id))).where(
            Boarding.flight_date >= start, Boarding.flight_date <= end
        )
    ) or 0

    # Recurring: passengers with 2+ boardings in window
    recurring_subq = (
        select(Boarding.passenger_id)
        .where(Boarding.flight_date >= start, Boarding.flight_date <= end)
        .group_by(Boarding.passenger_id)
        .having(func.count(Boarding.id) >= 2)
        .subquery()
    )
    recurring = db.scalar(select(func.count()).select_from(recurring_subq)) or 0

    return {
        "window_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "flights": flights,
        "boardings": boardings,
        "unique_passengers": unique,
        "recurring_passengers": recurring,
        "one_time_passengers": max(unique - recurring, 0),
        "recurrence_rate_pct": round((recurring / unique * 100), 1) if unique else 0,
    }


@app.get("/api/stats/monthly")
def stats_monthly(db: Session = Depends(get_db)) -> list[dict]:
    dialect = db.bind.dialect.name if db.bind else "sqlite"
    if dialect == "postgresql":
        month_expr = func.to_char(Boarding.flight_date, "YYYY-MM")
    else:
        month_expr = func.strftime("%Y-%m", Boarding.flight_date)

    rows = db.execute(
        select(
            month_expr.label("month"),
            func.count(Boarding.id).label("boardings"),
            func.count(func.distinct(Boarding.flight_id)).label("flights"),
            func.count(func.distinct(Boarding.passenger_id)).label("unique_passengers"),
        )
        .where(Boarding.flight_date.is_not(None))
        .group_by(month_expr)
        .order_by(month_expr)
    ).all()

    return [
        {
            "month": r.month,
            "boardings": r.boardings,
            "flights": r.flights,
            "unique_passengers": r.unique_passengers,
        }
        for r in rows
    ]


@app.get("/api/stats/top-routes")
def top_routes(limit: int = 15, days: int = 365, db: Session = Depends(get_db)) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=days)
    route = func.concat(
        func.coalesce(Boarding.origin_code, "?"),
        "→",
        func.coalesce(Boarding.dest_code, "?"),
    )
    rows = db.execute(
        select(route.label("route"), func.count(Boarding.id).label("boardings"))
        .where(Boarding.flight_date >= start, Boarding.flight_date <= end)
        .group_by(route)
        .order_by(func.count(Boarding.id).desc())
        .limit(limit)
    ).all()
    return [{"route": r.route, "boardings": r.boardings} for r in rows]


@app.get("/api/stats/top-passengers")
def top_passengers(
    limit: int = 20, days: int = 365, db: Session = Depends(get_db)
) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=days)
    rows = db.execute(
        select(
            Passenger.display_name,
            func.count(Boarding.id).label("flights"),
            func.count(func.distinct(Boarding.flight_date)).label("distinct_dates"),
        )
        .join(Boarding, Boarding.passenger_id == Passenger.id)
        .where(Boarding.flight_date >= start, Boarding.flight_date <= end)
        .group_by(Passenger.id, Passenger.display_name)
        .order_by(func.count(Boarding.id).desc())
        .limit(limit)
    ).all()
    return [
        {
            "name": r.display_name,
            "flights": r.flights,
            "distinct_dates": r.distinct_dates,
        }
        for r in rows
    ]


@app.get("/api/exports/boardings.csv")
def export_boardings_csv(db: Session = Depends(get_db)):
    from fastapi.responses import StreamingResponse
    import csv
    import io

    def generate():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "flight_date",
                "flight_time",
                "origin",
                "destination",
                "origin_code",
                "dest_code",
                "passenger_name",
                "document",
                "identity_key",
                "aircraft_reg",
                "source_file",
                "sheet_name",
            ]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        q = (
            select(
                Boarding.flight_date,
                Flight.flight_time,
                Flight.origin,
                Flight.destination,
                Boarding.origin_code,
                Boarding.dest_code,
                Boarding.passenger_name_raw,
                Boarding.document_raw,
                Passenger.identity_key,
                Flight.aircraft_reg,
                Flight.source_file,
                Flight.sheet_name,
            )
            .join(Flight, Flight.id == Boarding.flight_id)
            .join(Passenger, Passenger.id == Boarding.passenger_id)
            .order_by(Boarding.flight_date.desc(), Boarding.id)
        )
        for row in db.execute(q).yield_per(500):
            w.writerow(
                [
                    row.flight_date.isoformat() if row.flight_date else "",
                    row.flight_time or "",
                    row.origin or "",
                    row.destination or "",
                    row.origin_code or "",
                    row.dest_code or "",
                    row.passenger_name_raw,
                    row.document_raw or "",
                    row.identity_key,
                    row.aircraft_reg or "",
                    row.source_file,
                    row.sheet_name,
                ]
            )
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=boardings.csv"},
    )
