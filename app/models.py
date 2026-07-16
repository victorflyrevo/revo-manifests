from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class UploadBatch(Base):
    __tablename__ = "upload_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="processed", nullable=False)
    flights_found: Mapped[int] = mapped_column(Integer, default=0)
    flights_inserted: Mapped[int] = mapped_column(Integer, default=0)
    flights_skipped: Mapped[int] = mapped_column(Integer, default=0)
    boardings_inserted: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    flights: Mapped[list["Flight"]] = relationship(back_populates="upload")


class Flight(Base):
    __tablename__ = "flights"
    __table_args__ = (
        UniqueConstraint(
            "fingerprint",
            name="uq_flights_fingerprint",
        ),
        Index("ix_flights_date", "flight_date"),
        Index("ix_flights_route", "origin_code", "dest_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    upload_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("upload_batches.id", ondelete="SET NULL"), nullable=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_file: Mapped[str] = mapped_column(String(512), nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    flight_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    flight_time: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    origin: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    destination: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    origin_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    dest_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    aircraft_reg: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    aircraft_code: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    pax_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    upload: Mapped[Optional[UploadBatch]] = relationship(back_populates="flights")
    boardings: Mapped[list["Boarding"]] = relationship(
        back_populates="flight", cascade="all, delete-orphan"
    )


class Passenger(Base):
    """Long-lived passenger identity (document preferred, else normalized name)."""

    __tablename__ = "passengers"
    __table_args__ = (UniqueConstraint("identity_key", name="uq_passengers_identity"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    document_normalized: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    first_seen: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_seen: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    total_boardings: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    boardings: Mapped[list["Boarding"]] = relationship(back_populates="passenger")


class Boarding(Base):
    __tablename__ = "boardings"
    __table_args__ = (
        UniqueConstraint("flight_id", "passenger_id", name="uq_boarding_flight_pax"),
        Index("ix_boardings_date", "flight_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flight_id: Mapped[int] = mapped_column(
        ForeignKey("flights.id", ondelete="CASCADE"), nullable=False
    )
    passenger_id: Mapped[int] = mapped_column(
        ForeignKey("passengers.id", ondelete="CASCADE"), nullable=False
    )
    flight_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    passenger_name_raw: Mapped[str] = mapped_column(String(512), nullable=False)
    document_raw: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    origin_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    dest_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    flight: Mapped[Flight] = relationship(back_populates="boardings")
    passenger: Mapped[Passenger] = relationship(back_populates="boardings")
