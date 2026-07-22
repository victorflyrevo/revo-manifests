"""Integration tests for repairing already-ingested flights."""

from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.corrections import repair_existing_flights
from app.db import Base
from app.models import Boarding, Flight, Passenger


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_repair_fills_null_date_and_removes_cancelled() -> None:
    db = _session()
    good = Flight(
        fingerprint="fp-good",
        source_file="Nov-Dez_2025.xlsx",
        sheet_name="0101 OOE1 SSJ-SJK",
        flight_date=None,
        origin_code="SSJ",
        dest_code="SJK",
        pax_count=1,
    )
    cancelled = Flight(
        fingerprint="fp-cancel",
        source_file="Nov-Dez_2025.xlsx",
        sheet_name="Cancelado 0201 OOE1",
        flight_date=None,
        origin_code="SSJ",
        dest_code="SJK",
        pax_count=1,
    )
    db.add_all([good, cancelled])
    db.flush()

    pax = Passenger(
        identity_key="doc:ABC1234",
        display_name="Test Pax",
        document_normalized="ABC1234",
        total_boardings=2,
    )
    db.add(pax)
    db.flush()
    db.add_all(
        [
            Boarding(
                flight_id=good.id,
                passenger_id=pax.id,
                flight_date=None,
                passenger_name_raw="Test Pax",
                document_raw="ABC1234",
            ),
            Boarding(
                flight_id=cancelled.id,
                passenger_id=pax.id,
                flight_date=None,
                passenger_name_raw="Test Pax",
                document_raw="ABC1234",
            ),
        ]
    )
    db.commit()

    report = repair_existing_flights(db, dry_run=False)
    assert report.dates_fixed == 1
    assert report.cancelled_removed == 1

    remaining = list(db.scalars(select(Flight)).all())
    assert len(remaining) == 1
    assert remaining[0].flight_date == date(2026, 1, 1)
    boarding = db.scalar(select(Boarding))
    assert boarding is not None
    assert boarding.flight_date == date(2026, 1, 1)

    pax = db.scalar(select(Passenger))
    assert pax is not None
    assert pax.total_boardings == 1
    assert pax.first_seen == date(2026, 1, 1)
