"""Read-only reporting API for external dashboards / reports."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.corrections import (
    is_excluded_loop_flight,
    is_skippable_sheet,
    repair_existing_flights,
)
from app.dedupe import repair_near_duplicate_flights, run_hygiene_protocol
from app.db import get_db
from app.models import Boarding, Flight, Passenger, UploadBatch

router = APIRouter(
    prefix="/api/v1",
    tags=["report-api"],
    dependencies=[Depends(require_api_key)],
)


def _parse_date(value: Optional[str], name: str) -> Optional[date]:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid {name}; use YYYY-MM-DD") from exc


def _window(
    start_date: Optional[str],
    end_date: Optional[str],
    days: Optional[int],
) -> tuple[Optional[date], Optional[date]]:
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if days is not None and start is None and end is None:
        end = date.today()
        start = end - timedelta(days=days)
    if start and end and start > end:
        raise HTTPException(400, "start_date must be <= end_date")
    return start, end


@router.get("")
@router.get("/")
def api_index() -> dict:
    return {
        "name": "REVO Manifest Report API",
        "version": "v1",
        "auth": "Send header X-API-Key when API_KEY is configured",
        "endpoints": [
            "GET /api/v1/summary",
            "GET /api/v1/monthly",
            "GET /api/v1/routes",
            "GET /api/v1/passengers/top",
            "GET /api/v1/flights",
            "GET /api/v1/boardings",
            "GET /api/v1/passengers",
            "GET /api/v1/uploads",
            "GET /api/v1/export/boardings.csv",
            "POST /api/v1/repair/cancelled",
            "POST /api/v1/repair/dates",
            "POST /api/v1/repair/siav-loops",
            "POST /api/v1/repair/duplicates",
            "POST /api/v1/repair/all",
        ],
    }


@router.post("/repair/cancelled")
def purge_cancelled_flights(
    dry_run: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict:
    """Remove flights whose sheet name marks them as cancelled (API key auth).

    Defaults to dry_run=true. Set dry_run=false to apply deletes and fix
    passenger boarding counts.
    """
    report = repair_existing_flights(
        db,
        fix_null_dates=False,
        fix_inconsistent_dates=False,
        remove_cancelled=True,
        remove_siav_loops=False,
        dry_run=dry_run,
    )
    return report.as_dict()


@router.post("/repair/siav-loops")
def purge_siav_loop_flights(
    dry_run: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict:
    """Remove SIAV→SIAV training flights that have passengers (API key auth)."""
    report = repair_existing_flights(
        db,
        fix_null_dates=False,
        fix_inconsistent_dates=False,
        remove_cancelled=False,
        remove_siav_loops=True,
        dry_run=dry_run,
    )
    return report.as_dict()


@router.post("/repair/dates")
def repair_flight_dates_api(
    dry_run: bool = Query(True),
    fix_null_dates: bool = Query(True),
    fix_inconsistent_dates: bool = Query(True),
    remove_cancelled: bool = Query(False),
    remove_siav_loops: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict:
    """Fix null/inconsistent flight dates from sheet DDMM tokens (API key auth).

    Example: sheet ``3006`` + wrong cell month → ``2026-06-30``.
    """
    report = repair_existing_flights(
        db,
        fix_null_dates=fix_null_dates,
        fix_inconsistent_dates=fix_inconsistent_dates,
        remove_cancelled=remove_cancelled,
        remove_siav_loops=remove_siav_loops,
        dry_run=dry_run,
    )
    return report.as_dict()


@router.post("/repair/duplicates")
def repair_near_duplicates_api(
    dry_run: bool = Query(True),
    min_jaccard: float = Query(0.5, ge=0.3, le=1.0),
    db: Session = Depends(get_db),
) -> dict:
    """Remove near-duplicate flights (same slot + passenger overlap)."""
    report = repair_near_duplicate_flights(
        db, min_jaccard=min_jaccard, dry_run=dry_run
    )
    return report.as_dict()


@router.post("/repair/all")
def repair_all_hygiene(
    dry_run: bool = Query(True),
    min_jaccard: float = Query(0.5, ge=0.3, le=1.0),
    db: Session = Depends(get_db),
) -> dict:
    """Run the full clean-base protocol (cancelled, SIAV loops, dates, duplicates).

    Recommended after bulk uploads. Defaults to dry_run=true.
    """
    return run_hygiene_protocol(db, dry_run=dry_run, min_jaccard=min_jaccard)


@router.get("/summary")
def summary(
    days: Optional[int] = Query(None, ge=1, le=3650),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
) -> dict:
    start, end = _window(start_date, end_date, days if days is not None else 365)
    assert start is not None and end is not None

    flights = (
        db.scalar(
            select(func.count())
            .select_from(Flight)
            .where(Flight.flight_date >= start, Flight.flight_date <= end)
        )
        or 0
    )
    boardings = (
        db.scalar(
            select(func.count())
            .select_from(Boarding)
            .where(Boarding.flight_date >= start, Boarding.flight_date <= end)
        )
        or 0
    )
    unique = (
        db.scalar(
            select(func.count(func.distinct(Boarding.passenger_id))).where(
                Boarding.flight_date >= start, Boarding.flight_date <= end
            )
        )
        or 0
    )
    recurring_subq = (
        select(Boarding.passenger_id)
        .where(Boarding.flight_date >= start, Boarding.flight_date <= end)
        .group_by(Boarding.passenger_id)
        .having(func.count(Boarding.id) >= 2)
        .subquery()
    )
    recurring = db.scalar(select(func.count()).select_from(recurring_subq)) or 0

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "flights": flights,
        "boardings": boardings,
        "unique_passengers": unique,
        "recurring_passengers": recurring,
        "one_time_passengers": max(unique - recurring, 0),
        "recurrence_rate_pct": round((recurring / unique * 100), 1) if unique else 0.0,
        "avg_pax_per_flight": round(boardings / flights, 2) if flights else 0.0,
    }


@router.get("/monthly")
def monthly(db: Session = Depends(get_db)) -> list[dict]:
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


@router.get("/routes")
def routes(
    limit: int = Query(25, ge=1, le=500),
    days: Optional[int] = Query(None, ge=1, le=3650),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    start, end = _window(start_date, end_date, days if days is not None else 365)
    assert start is not None and end is not None
    route = func.concat(
        func.coalesce(Boarding.origin_code, "?"),
        "→",
        func.coalesce(Boarding.dest_code, "?"),
    )
    rows = db.execute(
        select(
            route.label("route"),
            func.count(Boarding.id).label("boardings"),
            func.count(func.distinct(Boarding.flight_id)).label("flights"),
        )
        .where(Boarding.flight_date >= start, Boarding.flight_date <= end)
        .group_by(route)
        .order_by(func.count(Boarding.id).desc())
        .limit(limit)
    ).all()
    return [
        {"route": r.route, "boardings": r.boardings, "flights": r.flights} for r in rows
    ]


@router.get("/passengers/top")
def passengers_top(
    limit: int = Query(25, ge=1, le=500),
    days: Optional[int] = Query(None, ge=1, le=3650),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    start, end = _window(start_date, end_date, days if days is not None else 365)
    assert start is not None and end is not None
    rows = db.execute(
        select(
            Passenger.id,
            Passenger.display_name,
            Passenger.identity_key,
            func.count(Boarding.id).label("boardings"),
            func.count(func.distinct(Boarding.flight_date)).label("distinct_dates"),
            func.min(Boarding.flight_date).label("first_in_window"),
            func.max(Boarding.flight_date).label("last_in_window"),
        )
        .join(Boarding, Boarding.passenger_id == Passenger.id)
        .where(Boarding.flight_date >= start, Boarding.flight_date <= end)
        .group_by(Passenger.id, Passenger.display_name, Passenger.identity_key)
        .order_by(func.count(Boarding.id).desc())
        .limit(limit)
    ).all()
    return [
        {
            "passenger_id": r.id,
            "name": r.display_name,
            "identity_key": r.identity_key,
            "boardings": r.boardings,
            "distinct_dates": r.distinct_dates,
            "first_in_window": r.first_in_window.isoformat() if r.first_in_window else None,
            "last_in_window": r.last_in_window.isoformat() if r.last_in_window else None,
        }
        for r in rows
    ]


@router.get("/flights")
def list_flights(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    origin: Optional[str] = None,
    destination: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    start, end = _window(start_date, end_date, None)
    q = select(Flight)
    count_q = select(func.count()).select_from(Flight)
    if start:
        q = q.where(Flight.flight_date >= start)
        count_q = count_q.where(Flight.flight_date >= start)
    if end:
        q = q.where(Flight.flight_date <= end)
        count_q = count_q.where(Flight.flight_date <= end)
    if origin:
        q = q.where(Flight.origin_code == origin.upper())
        count_q = count_q.where(Flight.origin_code == origin.upper())
    if destination:
        q = q.where(Flight.dest_code == destination.upper())
        count_q = count_q.where(Flight.dest_code == destination.upper())

    total = db.scalar(count_q) or 0
    rows = db.scalars(
        q.order_by(Flight.flight_date.desc(), Flight.id.desc()).limit(limit).offset(offset)
    ).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": f.id,
                "flight_date": f.flight_date.isoformat() if f.flight_date else None,
                "flight_time": f.flight_time,
                "origin": f.origin,
                "destination": f.destination,
                "origin_code": f.origin_code,
                "dest_code": f.dest_code,
                "aircraft_reg": f.aircraft_reg,
                "aircraft_code": f.aircraft_code,
                "pax_count": f.pax_count,
                "source_file": f.source_file,
                "sheet_name": f.sheet_name,
            }
            for f in rows
        ],
    }


@router.get("/boardings")
def list_boardings(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    passenger_id: Optional[int] = None,
    origin: Optional[str] = None,
    destination: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    start, end = _window(start_date, end_date, None)
    q = (
        select(
            Boarding.id,
            Boarding.flight_date,
            Boarding.passenger_name_raw,
            Boarding.document_raw,
            Boarding.origin_code,
            Boarding.dest_code,
            Boarding.passenger_id,
            Boarding.flight_id,
            Flight.flight_time,
            Flight.origin,
            Flight.destination,
            Flight.aircraft_reg,
            Flight.source_file,
            Flight.sheet_name,
            Passenger.identity_key,
            Passenger.display_name,
        )
        .join(Flight, Flight.id == Boarding.flight_id)
        .join(Passenger, Passenger.id == Boarding.passenger_id)
    )
    count_q = select(func.count()).select_from(Boarding)
    if start:
        q = q.where(Boarding.flight_date >= start)
        count_q = count_q.where(Boarding.flight_date >= start)
    if end:
        q = q.where(Boarding.flight_date <= end)
        count_q = count_q.where(Boarding.flight_date <= end)
    if passenger_id is not None:
        q = q.where(Boarding.passenger_id == passenger_id)
        count_q = count_q.where(Boarding.passenger_id == passenger_id)
    if origin:
        q = q.where(Boarding.origin_code == origin.upper())
        count_q = count_q.where(Boarding.origin_code == origin.upper())
    if destination:
        q = q.where(Boarding.dest_code == destination.upper())
        count_q = count_q.where(Boarding.dest_code == destination.upper())

    total = db.scalar(count_q) or 0
    rows = db.execute(
        q.order_by(Boarding.flight_date.desc(), Boarding.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": r.id,
                "flight_id": r.flight_id,
                "passenger_id": r.passenger_id,
                "flight_date": r.flight_date.isoformat() if r.flight_date else None,
                "flight_time": r.flight_time,
                "passenger_name": r.passenger_name_raw,
                "display_name": r.display_name,
                "document": r.document_raw,
                "identity_key": r.identity_key,
                "origin": r.origin,
                "destination": r.destination,
                "origin_code": r.origin_code,
                "dest_code": r.dest_code,
                "aircraft_reg": r.aircraft_reg,
                "source_file": r.source_file,
                "sheet_name": r.sheet_name,
            }
            for r in rows
        ],
    }


@router.get("/passengers")
def list_passengers(
    q: Optional[str] = Query(None, description="Search by name"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    query = select(Passenger)
    count_q = select(func.count()).select_from(Passenger)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(func.lower(Passenger.display_name).like(like))
        count_q = count_q.where(func.lower(Passenger.display_name).like(like))

    total = db.scalar(count_q) or 0
    rows = db.scalars(
        query.order_by(Passenger.total_boardings.desc(), Passenger.id)
        .limit(limit)
        .offset(offset)
    ).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": p.id,
                "display_name": p.display_name,
                "identity_key": p.identity_key,
                "document_normalized": p.document_normalized,
                "first_seen": p.first_seen.isoformat() if p.first_seen else None,
                "last_seen": p.last_seen.isoformat() if p.last_seen else None,
                "total_boardings": p.total_boardings,
            }
            for p in rows
        ],
    }


@router.get("/uploads")
def list_uploads(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.scalars(
        select(UploadBatch).order_by(UploadBatch.uploaded_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": u.id,
            "filename": u.filename,
            "uploaded_at": u.uploaded_at.isoformat() if u.uploaded_at else None,
            "status": u.status,
            "flights_found": u.flights_found,
            "flights_inserted": u.flights_inserted,
            "flights_skipped": u.flights_skipped,
            "boardings_inserted": u.boardings_inserted,
            "notes": u.notes,
        }
        for u in rows
    ]


@router.get("/export/boardings.csv")
def export_boardings_csv(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    import csv
    import io

    start, end = _window(start_date, end_date, None)

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
                "passenger_id",
                "flight_id",
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
                Boarding.passenger_id,
                Boarding.flight_id,
                Flight.aircraft_reg,
                Flight.source_file,
                Flight.sheet_name,
            )
            .join(Flight, Flight.id == Boarding.flight_id)
            .join(Passenger, Passenger.id == Boarding.passenger_id)
        )
        if start:
            q = q.where(Boarding.flight_date >= start)
        if end:
            q = q.where(Boarding.flight_date <= end)
        q = q.order_by(Boarding.flight_date.desc(), Boarding.id)

        for row in db.execute(q).yield_per(500):
            # Never export cancelled manifesto tabs
            if is_skippable_sheet(row.sheet_name or ""):
                continue
            if is_excluded_loop_flight(
                row.origin_code,
                row.dest_code,
                passenger_count=1,  # export rows are boardings ⇒ has passengers
            ):
                continue
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
                    row.passenger_id,
                    row.flight_id,
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
