"""Customer KPIs from the longest available boarding history.

Metrics (last twelve calendar months, or all months if shorter history):
- Cumulative growth of unique customers (first-ever boarding within the LTM window)
- Month-by-month repeat rate: share of LTM uniques who flew ≥2 times in that
  rolling 12-month window ending on each month
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Boarding


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, months: int) -> date:
    """Shift a date by whole months, clamping day to the target month length."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last = monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def month_end(d: date) -> date:
    return add_months(month_start(d), 1) - timedelta(days=1)


def month_label(d: date) -> str:
    return d.strftime("%Y-%m")


def iter_months(start: date, end: date) -> list[date]:
    """First day of each month from start..end inclusive."""
    cur = month_start(start)
    last = month_start(end)
    out: list[date] = []
    while cur <= last:
        out.append(cur)
        cur = add_months(cur, 1)
    return out


def _data_bounds(db: Session) -> tuple[Optional[date], Optional[date]]:
    row = db.execute(
        select(func.min(Boarding.flight_date), func.max(Boarding.flight_date)).where(
            Boarding.flight_date.is_not(None)
        )
    ).one()
    return row[0], row[1]


def compute_customer_kpis(db: Session, months: int = 12) -> dict:
    """Build customer KPI series using the full boarding history available."""
    data_start, data_end = _data_bounds(db)
    if data_start is None or data_end is None:
        return {
            "data_start": None,
            "data_end": None,
            "anchor_month": None,
            "ltm_start": None,
            "months_requested": months,
            "months_available": 0,
            "summary": {
                "unique_customers_ltm": 0,
                "repeat_customers_ltm": 0,
                "one_time_customers_ltm": 0,
                "repeat_rate_pct": 0.0,
                "new_customers_ltm": 0,
                "cumulative_unique_end": 0,
            },
            "monthly": [],
        }

    months = max(1, min(int(months), 120))
    anchor = month_start(data_end)
    # Prefer a full LTM ending on the latest data month; fall back to history start.
    ideal_ltm_start = add_months(anchor, -(months - 1))
    ltm_start = max(month_start(data_start), ideal_ltm_start)

    # Need boardings back another (months-1) before ltm_start for rolling windows,
    # but never before the earliest data.
    history_load_start = max(
        month_start(data_start),
        add_months(ltm_start, -(months - 1)),
    )

    first_seen_rows = db.execute(
        select(Boarding.passenger_id, func.min(Boarding.flight_date))
        .where(Boarding.flight_date.is_not(None))
        .group_by(Boarding.passenger_id)
    ).all()
    first_seen: dict[int, date] = {pid: fd for pid, fd in first_seen_rows if fd}

    boarding_rows = db.execute(
        select(Boarding.passenger_id, Boarding.flight_date).where(
            Boarding.flight_date.is_not(None),
            Boarding.flight_date >= history_load_start,
            Boarding.flight_date <= month_end(anchor),
        )
    ).all()

    # passenger -> sorted unique flight dates (boarding events by date still count
    # multiple flights on same day as distinct boardings via list, not set)
    pax_dates: dict[int, list[date]] = defaultdict(list)
    for pid, fd in boarding_rows:
        if fd is not None:
            pax_dates[pid].append(fd)

    series_months = iter_months(ltm_start, anchor)
    monthly: list[dict] = []
    cumulative = 0

    for m0 in series_months:
        m_end = month_end(m0)
        window_start = add_months(m0, -(months - 1))
        if window_start < month_start(data_start):
            window_start = month_start(data_start)

        new_customers = sum(
            1 for fd in first_seen.values() if month_start(fd) == m0
        )
        # Cumulative unique growth inside the displayed LTM window only
        cumulative += new_customers

        uniques = 0
        repeaters = 0
        for pid, dates in pax_dates.items():
            hits = [d for d in dates if window_start <= d <= m_end]
            if not hits:
                continue
            uniques += 1
            if len(hits) >= 2:
                repeaters += 1

        rate = round((repeaters / uniques * 100), 1) if uniques else 0.0
        monthly.append(
            {
                "month": month_label(m0),
                "window_start": window_start.isoformat(),
                "window_end": m_end.isoformat(),
                "new_customers": new_customers,
                "cumulative_unique_customers": cumulative,
                "ltm_unique_customers": uniques,
                "ltm_repeat_customers": repeaters,
                "ltm_one_time_customers": max(uniques - repeaters, 0),
                "repeat_rate_pct": rate,
            }
        )

    last = monthly[-1] if monthly else None
    # New customers in LTM = those with first_seen inside the displayed window
    new_in_ltm = sum(
        1 for fd in first_seen.values() if ltm_start <= month_start(fd) <= anchor
    )

    return {
        "data_start": data_start.isoformat(),
        "data_end": data_end.isoformat(),
        "anchor_month": month_label(anchor),
        "ltm_start": month_label(ltm_start),
        "months_requested": months,
        "months_available": len(monthly),
        "summary": {
            "unique_customers_ltm": last["ltm_unique_customers"] if last else 0,
            "repeat_customers_ltm": last["ltm_repeat_customers"] if last else 0,
            "one_time_customers_ltm": last["ltm_one_time_customers"] if last else 0,
            "repeat_rate_pct": last["repeat_rate_pct"] if last else 0.0,
            "new_customers_ltm": new_in_ltm,
            "cumulative_unique_end": last["cumulative_unique_customers"] if last else 0,
        },
        "monthly": monthly,
    }
