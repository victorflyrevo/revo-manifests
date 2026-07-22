"""Unit tests for customer LTM KPIs."""

from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.customer_kpis import compute_customer_kpis
from app.db import Base
from app.models import Boarding, Flight, Passenger


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db: Session) -> None:
    """
    History Jul 2024 → Jun 2025 (longest available).

    - Alice: Jul 2024, Aug 2024, Jan 2025  → repeater in late windows
    - Bob:   Jan 2025 only                 → one-time in Jan window
    - Cara:  Mar 2025, Apr 2025            → new + repeater
    - Dan:   Jun 2025                      → new one-time
    """
    people = {
        "alice": "Alice",
        "bob": "Bob",
        "cara": "Cara",
        "dan": "Dan",
    }
    pax = {}
    for key, name in people.items():
        p = Passenger(
            identity_key=f"doc:{key}",
            display_name=name,
            document_normalized=key.upper(),
            total_boardings=0,
        )
        db.add(p)
        db.flush()
        pax[key] = p

    flights_spec = [
        ("2024-07-10", "alice"),
        ("2024-08-12", "alice"),
        ("2025-01-05", "alice"),
        ("2025-01-20", "bob"),
        ("2025-03-03", "cara"),
        ("2025-04-08", "cara"),
        ("2025-06-15", "dan"),
    ]
    for i, (ds, who) in enumerate(flights_spec, start=1):
        fd = date.fromisoformat(ds)
        fl = Flight(
            fingerprint=f"fp-{i}",
            source_file="test.xlsx",
            sheet_name=f"{fd.day:02d}{fd.month:02d}",
            flight_date=fd,
            origin_code="SJK",
            dest_code="RAO",
            pax_count=1,
        )
        db.add(fl)
        db.flush()
        db.add(
            Boarding(
                flight_id=fl.id,
                passenger_id=pax[who].id,
                flight_date=fd,
                passenger_name_raw=pax[who].display_name,
                document_raw=who.upper(),
                origin_code="SJK",
                dest_code="RAO",
            )
        )
        pax[who].total_boardings = (pax[who].total_boardings or 0) + 1
        if pax[who].first_seen is None or fd < pax[who].first_seen:
            pax[who].first_seen = fd
        if pax[who].last_seen is None or fd > pax[who].last_seen:
            pax[who].last_seen = fd
    db.commit()


def test_empty_db():
    db = _session()
    out = compute_customer_kpis(db, months=12)
    assert out["months_available"] == 0
    assert out["summary"]["unique_customers_ltm"] == 0
    assert out["monthly"] == []


def test_uses_longest_history_and_ltm_metrics():
    db = _session()
    _seed(db)
    out = compute_customer_kpis(db, months=12)

    assert out["data_start"] == "2024-07-10"
    assert out["data_end"] == "2025-06-15"
    assert out["anchor_month"] == "2025-06"
    # From Jul 2024 through Jun 2025 = 12 months
    assert out["months_available"] == 12
    assert len(out["monthly"]) == 12

    by_month = {r["month"]: r for r in out["monthly"]}

    # July 2024: Alice first appears
    assert by_month["2024-07"]["new_customers"] == 1
    assert by_month["2024-07"]["cumulative_unique_customers"] == 1

    # January 2025: Bob is new; Alice already counted
    assert by_month["2025-01"]["new_customers"] == 1
    assert by_month["2025-01"]["cumulative_unique_customers"] == 2

    # June 2025 end: Alice, Bob, Cara, Dan all first-seen in window
    assert by_month["2025-06"]["new_customers"] == 1
    assert by_month["2025-06"]["cumulative_unique_customers"] == 4

    # Rolling LTM ending Jun 2025 covers Jul 2024–Jun 2025:
    # Alice 3, Bob 1, Cara 2, Dan 1 → 4 unique, 2 repeaters (Alice, Cara)
    last = by_month["2025-06"]
    assert last["ltm_unique_customers"] == 4
    assert last["ltm_repeat_customers"] == 2
    assert last["repeat_rate_pct"] == 50.0

    assert out["summary"]["unique_customers_ltm"] == 4
    assert out["summary"]["repeat_customers_ltm"] == 2
    assert out["summary"]["repeat_rate_pct"] == 50.0
    assert out["summary"]["new_customers_ltm"] == 4
    assert out["summary"]["cumulative_unique_end"] == 4


def test_shorter_history_uses_all_months():
    db = _session()
    p = Passenger(identity_key="doc:x", display_name="X", total_boardings=1)
    db.add(p)
    db.flush()
    fl = Flight(
        fingerprint="fp-x",
        source_file="t.xlsx",
        sheet_name="0105",
        flight_date=date(2026, 5, 1),
        pax_count=1,
    )
    db.add(fl)
    db.flush()
    db.add(
        Boarding(
            flight_id=fl.id,
            passenger_id=p.id,
            flight_date=date(2026, 5, 1),
            passenger_name_raw="X",
        )
    )
    db.commit()

    out = compute_customer_kpis(db, months=12)
    assert out["months_available"] == 1
    assert out["monthly"][0]["month"] == "2026-05"
    assert out["monthly"][0]["repeat_rate_pct"] == 0.0
