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


def _pax_names(db: Session, flight_id: int) -> set[str]:
    """Folded passenger names — catches splits where identity_key still differs."""
    from app.models import Boarding

    rows = db.execute(
        select(Boarding.passenger_name_raw).where(Boarding.flight_id == flight_id)
    ).all()
    return {_fold(r[0]).upper() for r in rows if r[0]}


def _passenger_overlap(keys_a: set[str], keys_b: set[str], names_a: set[str], names_b: set[str]) -> float:
    """Best of identity-key Jaccard and folded-name Jaccard."""
    return max(jaccard(keys_a, keys_b), jaccard(names_a, names_b))


def sheet_match_key(sheet_name: str) -> str:
    """Sheet identity that keeps OMB1 vs OMB2 (parallel legs) distinct."""
    sn = _fold(sheet_name or "")
    sn = re.sub(r"^c[oó]pia de\s+", "", sn)
    sn = re.sub(r"[^a-z0-9]+", " ", sn)
    return re.sub(r"\s+", " ", sn).strip()


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

    pax_keys: dict[int, set[str]] = {}
    pax_names: dict[int, set[str]] = {}
    for fl in flights:
        pax_keys[fl.id] = _pax_keys(db, fl.id)
        pax_names[fl.id] = _pax_names(db, fl.id)

    # Group by operational slot (ignore aircraft — re-exports often rename OMB/OOE)
    slots: dict[tuple, list[Any]] = {}
    for fl in flights:
        if not fl.flight_date:
            continue
        o = (fl.origin_code or "").strip().upper()
        d = (fl.dest_code or "").strip().upper()
        # Skip empty routes — ODS noise is handled by repair_reexport_duplicates
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
        if only_source_file and not any(
            fl.source_file == only_source_file for fl in group
        ):
            continue

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
                jac = _passenger_overlap(
                    pax_keys.get(a, set()),
                    pax_keys.get(b, set()),
                    pax_names.get(a, set()),
                    pax_names.get(b, set()),
                )
                same_sheet = canon_sheet(by_id[a].sheet_name) == canon_sheet(
                    by_id[b].sheet_name
                )
                if require_similar_sheet and not same_sheet:
                    continue
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
            members = [m for m in members if m not in removed_ids]
            if len(members) < 2:
                continue

            report.groups_found += 1
            ranked = sorted(
                members,
                key=lambda mid: _keep_score(
                    by_id[mid],
                    max(len(pax_keys.get(mid, set())), len(pax_names.get(mid, set()))),
                ),
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
                "kept_pax": len(pax_names.get(keep_id, set())),
                "removed": [
                    {
                        "flight_id": did,
                        "sheet": by_id[did].sheet_name,
                        "source": by_id[did].source_file,
                        "pax": len(pax_names.get(did, set())),
                        "jaccard_vs_kept": round(
                            _passenger_overlap(
                                pax_keys.get(did, set()),
                                pax_keys.get(keep_id, set()),
                                pax_names.get(did, set()),
                                pax_names.get(keep_id, set()),
                            ),
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
                report.boardings_removed += len(pax_names.get(did, set()))
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


def repair_reexport_duplicates(
    db: Session,
    *,
    min_jaccard: float = 0.7,
    dry_run: bool = True,
    sample_limit: int = 40,
) -> DedupeReport:
    """Remove re-export copies (esp. .ods) that match .xlsx on date+sheet+names.

    ODS uploads often fail to parse airport codes, so slot-based dedupe misses them.
    """
    from app.models import Flight

    report = DedupeReport(dry_run=dry_run)
    flights = list(db.scalars(select(Flight)).all())
    report.scanned_flights = len(flights)

    pax_keys = {fl.id: _pax_keys(db, fl.id) for fl in flights}
    pax_names = {fl.id: _pax_names(db, fl.id) for fl in flights}
    by_id = {fl.id: fl for fl in flights}

    # Index non-ods (prefer xlsx) by date + strict sheet key
    index: dict[tuple[str, str], list[int]] = {}
    ods_ids: list[int] = []
    for fl in flights:
        if not fl.flight_date:
            continue
        key = (fl.flight_date.isoformat(), sheet_match_key(fl.sheet_name))
        src = (fl.source_file or "").lower()
        if src.endswith(".ods"):
            ods_ids.append(fl.id)
        else:
            index.setdefault(key, []).append(fl.id)

    removed: set[int] = set()
    for oid in ods_ids:
        ofl = by_id[oid]
        key = (ofl.flight_date.isoformat(), sheet_match_key(ofl.sheet_name))
        candidates = index.get(key, [])
        if not candidates:
            # Fallback: loose canon_sheet (aircraft token ignored) against xlsx
            loose = canon_sheet(ofl.sheet_name)
            candidates = []
            for (dt, _sk), ids in index.items():
                if dt != key[0]:
                    continue
                for fid in ids:
                    if canon_sheet(by_id[fid].sheet_name) == loose:
                        candidates.append(fid)
        best_id = None
        best_jac = 0.0
        for cid in candidates:
            jac = _passenger_overlap(
                pax_keys.get(oid, set()),
                pax_keys.get(cid, set()),
                pax_names.get(oid, set()),
                pax_names.get(cid, set()),
            )
            if jac > best_jac:
                best_jac = jac
                best_id = cid
        if best_id is None or best_jac < min_jaccard:
            continue

        report.groups_found += 1
        report.flights_removed += 1
        report.boardings_removed += len(pax_names.get(oid, set()))
        removed.add(oid)
        if len(report.samples) < sample_limit:
            report.samples.append(
                {
                    "action": "remove_ods_reexport",
                    "removed_flight_id": oid,
                    "removed_source": ofl.source_file,
                    "removed_sheet": ofl.sheet_name,
                    "kept_flight_id": best_id,
                    "kept_source": by_id[best_id].source_file,
                    "kept_sheet": by_id[best_id].sheet_name,
                    "jaccard": round(best_jac, 2),
                    "date": ofl.flight_date.isoformat(),
                }
            )
        if not dry_run:
            fl = db.get(Flight, oid)
            if fl is not None:
                _delete_flight(db, fl)

    # Cross-file re-exports with same date + sheet_match_key + high name overlap
    # (e.g. boundary month files) even when times differ slightly.
    groups: dict[tuple[str, str], list[int]] = {}
    for fl in flights:
        if fl.id in removed or not fl.flight_date:
            continue
        key = (fl.flight_date.isoformat(), sheet_match_key(fl.sheet_name))
        groups.setdefault(key, []).append(fl.id)

    for key, members in groups.items():
        members = [m for m in members if m not in removed]
        if len(members) < 2:
            continue
        sources = {by_id[m].source_file for m in members}
        if len(sources) < 2:
            continue
        ranked = sorted(
            members,
            key=lambda mid: _keep_score(
                by_id[mid],
                max(len(pax_keys.get(mid, set())), len(pax_names.get(mid, set()))),
            ),
            reverse=True,
        )
        keep = ranked[0]
        for did in ranked[1:]:
            if by_id[did].source_file == by_id[keep].source_file:
                continue
            jac = _passenger_overlap(
                pax_keys.get(did, set()),
                pax_keys.get(keep, set()),
                pax_names.get(did, set()),
                pax_names.get(keep, set()),
            )
            if jac < min_jaccard:
                continue
            report.groups_found += 1
            report.flights_removed += 1
            report.boardings_removed += len(pax_names.get(did, set()))
            removed.add(did)
            if len(report.samples) < sample_limit:
                report.samples.append(
                    {
                        "action": "remove_cross_file_reexport",
                        "date": key[0],
                        "sheet_key": key[1],
                        "kept_flight_id": keep,
                        "kept_source": by_id[keep].source_file,
                        "removed_flight_id": did,
                        "removed_source": by_id[did].source_file,
                        "jaccard": round(jac, 2),
                    }
                )
            if not dry_run:
                fl = db.get(Flight, did)
                if fl is not None:
                    _delete_flight(db, fl)

    if not dry_run:
        db.commit()
    else:
        db.rollback()
    return report


def repair_empty_flights(
    db: Session,
    *,
    dry_run: bool = True,
    sample_limit: int = 40,
) -> DedupeReport:
    """Delete flights with zero boardings (blank/index sheets)."""
    from app.models import Boarding, Flight

    report = DedupeReport(dry_run=dry_run)
    flights = list(db.scalars(select(Flight)).all())
    report.scanned_flights = len(flights)
    for fl in flights:
        n = db.scalar(
            select(Boarding.id).where(Boarding.flight_id == fl.id).limit(1)
        )
        if n is not None:
            continue
        report.groups_found += 1
        report.flights_removed += 1
        if len(report.samples) < sample_limit:
            report.samples.append(
                {
                    "action": "remove_empty_flight",
                    "flight_id": fl.id,
                    "source": fl.source_file,
                    "sheet": fl.sheet_name,
                    "date": fl.flight_date.isoformat() if fl.flight_date else None,
                }
            )
        if not dry_run:
            db.delete(fl)
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
    """Full clean-base protocol: cancelled → SIAV → dates → flight dupes → identities."""
    from app.corrections import repair_existing_flights
    from app.identity import repair_merge_split_identities

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
    empty = repair_empty_flights(db, dry_run=dry_run)
    reexports = repair_reexport_duplicates(db, dry_run=dry_run)
    dupes = repair_near_duplicate_flights(
        db, min_jaccard=min_jaccard, dry_run=dry_run
    )
    identities = repair_merge_split_identities(db, dry_run=dry_run)
    return {
        "dry_run": dry_run,
        "protocol": [
            "1. remove cancelled sheets",
            "2. remove SIAV→SIAV training flights with passengers",
            "3. fix sheet-DDMM date inconsistencies / wrong-dated duplicates",
            "4. remove empty (0-pax) flights",
            "5. remove .ods / cross-file re-exports (same date+sheet+names)",
            "6. remove near-duplicate flight overlaps (slot + name/key Jaccard)",
            "7. merge split passenger identities (same CPF/doc, compatible names)",
        ],
        "cancelled": cancelled.as_dict(),
        "siav_loops": siav.as_dict(),
        "dates": dates.as_dict(),
        "empty_flights": empty.as_dict(),
        "reexports": reexports.as_dict(),
        "near_duplicates": dupes.as_dict(),
        "identities": identities.as_dict(),
        "totals": {
            "cancelled_removed": cancelled.cancelled_removed,
            "siav_loops_removed": siav.siav_loops_removed,
            "dates_fixed": dates.dates_fixed,
            "date_duplicates_removed": dates.duplicates_removed,
            "empty_flights_removed": empty.flights_removed,
            "reexport_flights_removed": reexports.flights_removed,
            "near_duplicate_flights_removed": dupes.flights_removed,
            "near_duplicate_boardings_removed": dupes.boardings_removed,
            "passengers_merged": identities.passengers_merged,
            "identity_groups_merged": identities.groups_found,
            "identity_unsafe_skipped": identities.skipped_unsafe,
        },
    }
