"""Tests for document canonicalization and identity merges."""

from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.identity import (
    canonical_document,
    identity_key,
    name_similarity,
    repair_merge_split_identities,
)
from app.models import Boarding, Flight, Passenger
from app.parser import identity_key as parser_identity_key


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_canonical_document_collapses_cpf_variants() -> None:
    assert canonical_document("022.863.048-74") == "CPF02286304874"
    assert canonical_document("CPF - 022.863.048-74") == "CPF02286304874"
    assert canonical_document("CPF:022.863.048-74") == "CPF02286304874"
    assert identity_key("Najla", "44178106805")[0] == identity_key(
        "NAJLA FARES", "CPF 441.781.068-05"
    )[0]
    assert parser_identity_key("X", "CPF26714955871")[0] == "doc:CPF26714955871"


def test_passport_plus_cpf_uses_cpf() -> None:
    assert (
        canonical_document("PSPT: FV660046 - CPF 070.723.617-74") == "CPF07072361774"
    )


def test_name_similarity_typo_lastname() -> None:
    assert name_similarity("Felipe Calbucci", "Felipe Cabulcci") >= 0.75


def test_merge_cpf_split_passengers() -> None:
    db = _session()
    a = Passenger(
        identity_key="doc:26714955871",
        display_name="Agamenon Rocha Machado Junior",
        document_normalized="26714955871",
        total_boardings=2,
    )
    b = Passenger(
        identity_key="doc:CPF26714955871",
        display_name="Agamenon Rocha Machado Junior",
        document_normalized="CPF26714955871",
        total_boardings=1,
    )
    db.add_all([a, b])
    db.flush()
    fl1 = Flight(
        fingerprint="f1",
        source_file="a.xlsx",
        sheet_name="0101",
        flight_date=date(2025, 1, 1),
        origin_code="SBGR",
        dest_code="SDXQ",
        pax_count=1,
    )
    fl2 = Flight(
        fingerprint="f2",
        source_file="b.xlsx",
        sheet_name="0201",
        flight_date=date(2025, 1, 2),
        origin_code="SBGR",
        dest_code="SDXQ",
        pax_count=1,
    )
    db.add_all([fl1, fl2])
    db.flush()
    db.add_all(
        [
            Boarding(
                flight_id=fl1.id,
                passenger_id=a.id,
                flight_date=date(2025, 1, 1),
                passenger_name_raw=a.display_name,
            ),
            Boarding(
                flight_id=fl2.id,
                passenger_id=b.id,
                flight_date=date(2025, 1, 2),
                passenger_name_raw=b.display_name,
            ),
        ]
    )
    db.commit()

    report = repair_merge_split_identities(db, dry_run=False)
    assert report.groups_found == 1
    assert report.passengers_merged == 1
    remaining = list(db.scalars(select(Passenger)).all())
    assert len(remaining) == 1
    assert remaining[0].identity_key == "doc:CPF26714955871"
    assert remaining[0].total_boardings == 2
