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


def test_repair_removes_wrong_dated_duplicate() -> None:
    """When the correct date already exists, drop the bad-dated copy."""
    db = _session()
    # Correct flight already at 2024-12-28 for sheet 2812
    correct = Flight(
        fingerprint="fp-correct-2812",
        source_file="Manifesto REVO Jan-Abr_2025-2.xlsx",
        sheet_name="2812 SDXQ x SBGR OMH",
        flight_date=date(2024, 12, 28),
        origin_code="SDXQ",
        dest_code="SBGR",
        flight_time="10:00",
        pax_count=1,
    )
    wrong = Flight(
        fingerprint="fp-wrong-2812",
        source_file="Manifesto REVO Jan-Abr_2025-2.xlsx",
        sheet_name="2812 SDXQ x SBGR OMH",
        flight_date=date(2025, 2, 28),
        origin_code="SDXQ",
        dest_code="SBGR",
        flight_time="10:00",
        pax_count=1,
    )
    db.add_all([correct, wrong])
    db.flush()

    # Same passengers → same operational fingerprint when date is fixed
    pax = Passenger(
        identity_key="doc:XYZ9999",
        display_name="Dup Pax",
        document_normalized="XYZ9999",
        total_boardings=2,
    )
    db.add(pax)
    db.flush()
    db.add_all(
        [
            Boarding(
                flight_id=correct.id,
                passenger_id=pax.id,
                flight_date=date(2024, 12, 28),
                passenger_name_raw="Dup Pax",
                document_raw="XYZ9999",
            ),
            Boarding(
                flight_id=wrong.id,
                passenger_id=pax.id,
                flight_date=date(2025, 2, 28),
                passenger_name_raw="Dup Pax",
                document_raw="XYZ9999",
            ),
        ]
    )
    db.commit()

    # Align fingerprints with parser so collision is detected
    from app.parser import ParsedFlight, ParsedPassenger, flight_fingerprint

    for fl in (correct, wrong):
        parsed = ParsedFlight(
            sheet_name=fl.sheet_name,
            flight_date=fl.flight_date,
            flight_time=fl.flight_time,
            origin=None,
            destination=None,
            origin_code=fl.origin_code,
            dest_code=fl.dest_code,
            aircraft_reg=None,
            aircraft_code=None,
            passengers=[
                ParsedPassenger(
                    name="Dup Pax",
                    document="XYZ9999",
                    identity_key="doc:XYZ9999",
                    document_normalized="XYZ9999",
                )
            ],
        )
        fl.fingerprint = flight_fingerprint(parsed, fl.source_file)
    db.commit()

    report = repair_existing_flights(db, dry_run=False, remove_cancelled=False)
    assert report.duplicates_removed == 1

    remaining = list(db.scalars(select(Flight)).all())
    assert len(remaining) == 1
    assert remaining[0].flight_date == date(2024, 12, 28)
    pax = db.scalar(select(Passenger))
    assert pax is not None
    assert pax.total_boardings == 1
