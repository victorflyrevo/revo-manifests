"""Tests for near-duplicate flight hygiene."""

from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.dedupe import canon_sheet, repair_near_duplicate_flights, run_hygiene_protocol
from app.models import Boarding, Flight, Passenger


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_canon_sheet_ignores_aircraft_suffix() -> None:
    assert canon_sheet("1712 SDXQ x SBGR OMB") == canon_sheet("1712 SDXQ x SBGR OOE")
    assert canon_sheet("Cópia de 1610 CAPSUxSDXQ") == canon_sheet("1610 CAPSUxSDXQ")


def test_repair_removes_cross_file_overlap() -> None:
    db = _session()
    a = Flight(
        fingerprint="fp-a",
        source_file="Manifesto REVO Novo Out24.xlsx",
        sheet_name="2112 SDXQ x SBOP OMH",
        flight_date=date(2024, 12, 21),
        flight_time="19:15",
        origin_code="SDXQ",
        dest_code="SBOP",
        pax_count=3,
    )
    b = Flight(
        fingerprint="fp-b",
        source_file="Manifesto REVO Jan-Abr_2025-2.xlsx",
        sheet_name="2112 SDXQ x SBOP OMH",
        flight_date=date(2024, 12, 21),
        flight_time="19:15",
        origin_code="SDXQ",
        dest_code="SBOP",
        pax_count=3,
    )
    db.add_all([a, b])
    db.flush()

    keys = ["doc:A1", "doc:A2", "doc:A3"]
    for i, key in enumerate(keys):
        pax = Passenger(
            identity_key=key,
            display_name=f"Pax {i}",
            document_normalized=key.split(":")[1],
            total_boardings=2,
        )
        db.add(pax)
        db.flush()
        db.add_all(
            [
                Boarding(
                    flight_id=a.id,
                    passenger_id=pax.id,
                    flight_date=date(2024, 12, 21),
                    passenger_name_raw=pax.display_name,
                ),
                Boarding(
                    flight_id=b.id,
                    passenger_id=pax.id,
                    flight_date=date(2024, 12, 21),
                    passenger_name_raw=pax.display_name,
                ),
            ]
        )
    # Make b slightly smaller so a is kept
    # (equal pax → lower id kept via -id score; a has lower id)
    db.commit()

    report = repair_near_duplicate_flights(db, dry_run=False, min_jaccard=0.5)
    assert report.groups_found == 1
    assert report.flights_removed == 1
    remaining = list(db.scalars(select(Flight)).all())
    assert len(remaining) == 1
    assert remaining[0].id == a.id


def test_hygiene_protocol_dry_run() -> None:
    db = _session()
    out = run_hygiene_protocol(db, dry_run=True)
    assert out["dry_run"] is True
    assert "near_duplicates" in out
    assert out["totals"]["near_duplicate_flights_removed"] == 0
