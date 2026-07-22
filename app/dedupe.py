"""Cross-file / near-duplicate flight cleanup for a clean manifesto database.

Protocol (keep in sync with /api/v1/repair/all):
1. Skip cancelled sheets on ingest
2. Skip SIAV→SIAV training flights with passengers on ingest
3. Exact fingerprint + content-hash dedup on ingest
4. After ingest / on repair: merge near-duplicates that share the same
   operational slot (date+route+time) with high passenger overlap
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


def canon_sheet(sheet_name: str) -> str:
    """Normalize sheet titles so '1712 SDXQ x SBGR OMB' ≈ '1712 SDXQ x SBGR OOE'."""
    sn = _fold(sheet_name or "")
    sn = re.sub(r"^c[oó]pia de\s+", "", sn)
    # Drop trailing aircraft tokens that often differ across re-exports
    sn = re.sub(r"\b(ooe|omb|omh)\d*\b", " ", sn)
    sn = re.sub(r"[^a-z0-9]+", " ", sn)
    return re.sub(r"\s+", " ", sn).strip()


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class DedupeReport:
    scanned_flights: int = 0
    groups_found: int = 0
    flights_removed: int = 0
    boardings_removed: int = 0
    dry_run: bool = False
    samples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned_flights": self.scanned_flights,
            "groups_found": self.groups_found,
            "flights_removed": self.flights_removed,
            "boardings_removed": self.boardings_removed,
            "dry_run": self.dry_run,
            "samples": self.samples,
        }


def _pax_keys(db: Session, flight_id: int) -> set[str]:
    from app.models import Boarding, Passenger

    rows = db.execute(
        select(Passenger.identity_key)
        .join(Boarding, Boarding.passenger_id == Passenger.id)
        .where(Boarding.flight_id == flight_id)
    ).all()
    return {r[0] for r in rows if r[0]}


def _delete_flight(db: Session, flight: Any) -> int:
    """Delete flight and recount affected passengers. Returns boarding count removed."""
    from app.models import Boarding, Passenger

    boardings = list(
        db.scalars(select(Boarding).where(Boarding.flight_id == flight.id)).all()
    )
    pax_ids = [b.passenger_id for b in boardings]
    n = len(boardings)
    db.delete(flight)
    db.flush()
    for pid in set(pax_ids):
        pax = db.get(Passenger, pid)
        if not pax:
            continue
        remaining = list(
            db.scalars(select(Boarding).where(Boarding.passenger_id == pid)).all()
        )
        pax.total_boardings = len(remaining)
        dates = [b.flight_date for b in remaining if b.flight_date]
        pax.first_seen = min(dates) if dates else None
        pax.last_seen = max(dates) if dates else None
    return n


def _keep_score(fl: Any, pax_count: int) -> tuple:
    """Higher score wins. Prefer richer manifests, then older id (first ingest)."""
    sheet_has_ddmm = 1 if re.match(r"^\d{4}", (fl.sheet_name or "").strip()) else 0
    return (pax_count, sheet_has_ddmm, -int(fl.id))


def repair_near_duplicate_flights(
    db: Session,
    *,
    min_jaccard: float = 0.5,
    require_similar_sheet: bool = False,
    dry_run: bool = True,
    sample_limit: int = 40,
    only_source_file: Optional[str] = None,
) -> DedupeReport:
    """Remove near-duplicate flights that overlap on slot + passengers.

    Two flights are duplicates when they share the same
    (flight_date, origin_code, dest_code, flight_time) and their passenger
    identity sets overlap with Jaccard ≥ min_jaccard.

    When require_similar_sheet is True, also require matching canon_sheet().
    """
    from app.models import Boarding, Flight

    report = DedupeReport(dry_run=dry_run)
    flights = list(db.scalars(select(Flight)).all())
    report.scanned_flights = len(flights)

    # Preload pax keys
    pax_map: dict[int, set[str]] = {}
    for fl in flights:
        pax_map[fl.id] = _pax_keys(db, fl.id)

    # Group by operational slot (ignore aircraft — re-exports often rename OMB/OOE)
    slots: dict[tuple, list[Any]] = {}
    for fl in flights:
        if not fl.flight_date:
            continue
        o = (fl.origin_code or "").strip().upper()
        d = (fl.dest_code or "").strip().upper()
        # Skip empty routes — ODS noise produces fake collisions
        if not o and not d:
            continue
        key = (
            fl.flight_date.isoformat(),
            o,
            d,
            (fl.flight_time or "").strip(),
        )
        slots.setdefault(key, []).append(fl)

    removed_ids: set[int] = set()

    for slot, group in slots.items():
        if len(group) < 2:
            continue
        # Optional filter: only groups touching a given source file
        if only_source_file and not any(
            fl.source_file == only_source_file for fl in group
        ):
            continue

        # Build pairwise duplicate edges, then connected components
        ids = [fl.id for fl in group]
        by_id = {fl.id: fl for fl in group}
        parent = {i: i for i in ids}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        linked = False
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                sa, sb = pax_map.get(a, set()), pax_map.get(b, set())
                jac = jaccard(sa, sb)
                same_sheet = canon_sheet(by_id[a].sheet_name) == canon_sheet(
                    by_id[b].sheet_name
                )
                if require_similar_sheet and not same_sheet:
                    continue
                # Same normalized sheet → lower bar; different sheet → stricter
                threshold = min_jaccard if same_sheet else max(min_jaccard, 0.7)
                if jac < threshold:
                    continue
                union(a, b)
                linked = True

        if not linked:
            continue

        components: dict[int, list[int]] = {}
        for i in ids:
            components.setdefault(find(i), []).append(i)

        for members in components.values():
            if len(members) < 2:
                continue
            # Drop already-removed
            members = [m for m in members if m not in removed_ids]
            if len(members) < 2:
                continue

            report.groups_found += 1
            ranked = sorted(
                members,
                key=lambda mid: _keep_score(by_id[mid], len(pax_map.get(mid, set()))),
                reverse=True,
            )
            keep_id = ranked[0]
            drop_ids = ranked[1:]

            sample = {
                "action": "remove_near_duplicates",
                "slot": {
                    "date": slot[0],
                    "origin": slot[1],
                    "dest": slot[2],
                    "time": slot[3],
                },
                "kept_flight_id": keep_id,
                "kept_sheet": by_id[keep_id].sheet_name,
                "kept_source": by_id[keep_id].source_file,
                "kept_pax": len(pax_map.get(keep_id, set())),
                "removed": [
                    {
                        "flight_id": did,
                        "sheet": by_id[did].sheet_name,
                        "source": by_id[did].source_file,
                        "pax": len(pax_map.get(did, set())),
                        "jaccard_vs_kept": round(
                            jaccard(pax_map.get(did, set()), pax_map.get(keep_id, set())),
                            2,
                        ),
                    }
                    for did in drop_ids
                ],
            }
            if len(report.samples) < sample_limit:
                report.samples.append(sample)

            for did in drop_ids:
                report.flights_removed += 1
                report.boardings_removed += len(pax_map.get(did, set()))
                removed_ids.add(did)
                if not dry_run:
                    fl = db.get(Flight, did)
                    if fl is not None:
                        _delete_flight(db, fl)

    if not dry_run:
        db.commit()
    else:
        db.rollback()
    return report


def run_hygiene_protocol(
    db: Session,
    *,
    dry_run: bool = True,
    min_jaccard: float = 0.5,
) -> dict[str, Any]:
    """Full clean-base protocol: cancelled → SIAV loops → dates → near-dupes."""
    from app.corrections import repair_existing_flights

    cancelled = repair_existing_flights(
        db,
        fix_null_dates=False,
        fix_inconsistent_dates=False,
        remove_cancelled=True,
        remove_siav_loops=False,
        dry_run=dry_run,
    )
    siav = repair_existing_flights(
        db,
        fix_null_dates=False,
        fix_inconsistent_dates=False,
        remove_cancelled=False,
        remove_siav_loops=True,
        dry_run=dry_run,
    )
    dates = repair_existing_flights(
        db,
        fix_null_dates=True,
        fix_inconsistent_dates=True,
        remove_cancelled=False,
        remove_siav_loops=False,
        dry_run=dry_run,
    )
    dupes = repair_near_duplicate_flights(
        db, min_jaccard=min_jaccard, dry_run=dry_run
    )
    return {
        "dry_run": dry_run,
        "protocol": [
            "1. remove cancelled sheets",
            "2. remove SIAV→SIAV training flights with passengers",
            "3. fix sheet-DDMM date inconsistencies / wrong-dated duplicates",
            "4. remove near-duplicate overlaps (same slot + passenger Jaccard)",
        ],
        "cancelled": cancelled.as_dict(),
        "siav_loops": siav.as_dict(),
        "dates": dates.as_dict(),
        "near_duplicates": dupes.as_dict(),
        "totals": {
            "cancelled_removed": cancelled.cancelled_removed,
            "siav_loops_removed": siav.siav_loops_removed,
            "dates_fixed": dates.dates_fixed,
            "date_duplicates_removed": dates.duplicates_removed,
            "near_duplicate_flights_removed": dupes.flights_removed,
            "near_duplicate_boardings_removed": dupes.boardings_removed,
        },
    }
