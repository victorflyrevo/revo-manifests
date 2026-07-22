from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.corrections import annotate_gap_hint, is_excluded_loop_flight
from app.dedupe import repair_near_duplicate_flights
from app.models import Boarding, Flight, Passenger, UploadBatch
from app.parser import AIRPORT_CODE_MAX, ParseResult, content_hash, parse_bytes


def _clip_code(value: str | None, max_len: int = AIRPORT_CODE_MAX) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]

# Commit periodically so large workbooks (hundreds of sheets) don't hold one giant txn
COMMIT_EVERY = 50


def ingest_workbook(db: Session, data: bytes, filename: str) -> UploadBatch:
    digest = content_hash(data)
    existing = db.scalar(
        select(UploadBatch).where(
            UploadBatch.content_hash == digest,
            UploadBatch.status == "processed",
        )
    )
    if existing:
        existing.notes = "File already ingested (same content hash); skipped re-parse."
        db.commit()
        db.refresh(existing)
        return existing

    parsed: ParseResult = parse_bytes(data, filename)
    batch = UploadBatch(
        filename=filename,
        content_hash=digest,
        status="processing",
        flights_found=len(parsed.flights),
    )
    db.add(batch)
    db.flush()

    inserted = skipped = boardings = 0
    since_commit = 0

    for fl in parsed.flights:
        origin_code = _clip_code(fl.origin_code)
        dest_code = _clip_code(fl.dest_code)
        # Business rule: SIAV→SIAV with passengers = training, never count
        if is_excluded_loop_flight(
            origin_code, dest_code, passenger_count=len(fl.passengers)
        ):
            skipped += 1
            continue

        prior = db.scalar(select(Flight.id).where(Flight.fingerprint == fl.fingerprint))
        if prior:
            skipped += 1
            continue

        flight = Flight(
            upload_id=batch.id,
            fingerprint=fl.fingerprint,
            source_file=filename,
            sheet_name=fl.sheet_name[:255] if fl.sheet_name else fl.sheet_name,
            flight_date=fl.flight_date,
            flight_time=(fl.flight_time or "")[:16] or None,
            origin=(fl.origin or "")[:255] or None,
            destination=(fl.destination or "")[:255] or None,
            origin_code=origin_code,
            dest_code=dest_code,
            aircraft_reg=(fl.aircraft_reg or "")[:32] or None,
            aircraft_code=(fl.aircraft_code or "")[:16] or None,
            pax_count=len(fl.passengers),
        )
        db.add(flight)
        db.flush()
        inserted += 1

        seen_on_flight: set[int] = set()
        for pax in fl.passengers:
            passenger = db.scalar(
                select(Passenger).where(Passenger.identity_key == pax.identity_key)
            )
            if not passenger:
                passenger = Passenger(
                    identity_key=pax.identity_key,
                    display_name=pax.name,
                    document_normalized=pax.document_normalized,
                    first_seen=fl.flight_date,
                    last_seen=fl.flight_date,
                    total_boardings=0,
                )
                db.add(passenger)
                db.flush()
            else:
                if fl.flight_date:
                    if passenger.first_seen is None or fl.flight_date < passenger.first_seen:
                        passenger.first_seen = fl.flight_date
                    if passenger.last_seen is None or fl.flight_date > passenger.last_seen:
                        passenger.last_seen = fl.flight_date
                if len(pax.name) > len(passenger.display_name or ""):
                    passenger.display_name = pax.name

            if passenger.id in seen_on_flight:
                continue
            seen_on_flight.add(passenger.id)

            db.add(
                Boarding(
                    flight_id=flight.id,
                    passenger_id=passenger.id,
                    flight_date=fl.flight_date,
                    passenger_name_raw=pax.name[:512],
                    document_raw=(pax.document or "")[:255] or None,
                    origin_code=origin_code,
                    dest_code=dest_code,
                )
            )
            passenger.total_boardings = (passenger.total_boardings or 0) + 1
            boardings += 1

        flight.pax_count = len(seen_on_flight)
        since_commit += 1
        if since_commit >= COMMIT_EVERY:
            batch.flights_inserted = inserted
            batch.flights_skipped = skipped
            batch.boardings_inserted = boardings
            db.commit()
            # re-attach batch after commit
            batch = db.get(UploadBatch, batch.id)  # type: ignore[assignment]
            since_commit = 0

    batch.flights_inserted = inserted
    batch.flights_skipped = skipped
    batch.boardings_inserted = boardings
    batch.status = "processed"
    lower = filename.lower()
    if lower.endswith(".csv"):
        kind = "CSV"
    elif lower.endswith(".ods"):
        kind = "ODS"
    else:
        kind = "workbook"
    last_date = None
    for fl in parsed.flights:
        if fl.flight_date and (last_date is None or fl.flight_date > last_date):
            last_date = fl.flight_date
    notes = [
        f"Processed {kind}: {len(parsed.flights)} flight(s); "
        f"skipped {parsed.skipped_sheets} template/cancelled sheet(s)."
    ]
    gap = annotate_gap_hint(filename, last_date)
    if gap:
        notes.append(gap)

    # Hygiene: collapse near-duplicates that overlap this upload with prior data
    dedupe = repair_near_duplicate_flights(
        db,
        min_jaccard=0.5,
        dry_run=False,
        only_source_file=filename,
        sample_limit=5,
    )
    if dedupe.flights_removed:
        notes.append(
            f"Hygiene removed {dedupe.flights_removed} near-duplicate flight(s) "
            f"({dedupe.boardings_removed} boarding overlap(s))."
        )
        # Refresh batch counters after deletes (inserted stays historical for this run)
        batch = db.get(UploadBatch, batch.id)  # type: ignore[assignment]

    batch.notes = " ".join(notes)
    db.commit()
    db.refresh(batch)
    return batch
