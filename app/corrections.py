"""Correct common manifesto upload quirks before data lands in the database.

Handles:
- Cancelled / template sheets that should not become flights
- Sheet names with prefixes (Cancelado, Cópia de) or separators (DD-MM, DD/MM)
- Year resolution for bi-month files that cross a year boundary
  (e.g. Nov-Dez_2025 sheet 0101 → 2026-01-01)
- Repair of already-ingested rows with null / inconsistent dates
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

# Portuguese month tokens as they appear in REVO filenames
_MONTH_PT: dict[str, int] = {
    "jan": 1,
    "janeiro": 1,
    "fev": 2,
    "fevereiro": 2,
    "mar": 3,
    "marco": 3,
    "março": 3,
    "abr": 4,
    "abril": 4,
    "mai": 5,
    "maio": 5,
    "jun": 6,
    "junho": 6,
    "jul": 7,
    "julho": 7,
    "ago": 8,
    "agosto": 8,
    "set": 9,
    "setembro": 9,
    "out": 10,
    "outubro": 10,
    "nov": 11,
    "novembro": 11,
    "dez": 12,
    "dezembro": 12,
}

# Sheets that must never become flights
SKIP_SHEET_PREFIXES: tuple[str, ...] = (
    "base de dados",
    "xxxx",
    "cancelado",
    "cancelada",
    "cancelados",
    "canceladas",
    "cancelled",
    "canceled",
)

# Soft prefixes stripped before reading DDMM from the sheet title
_SHEET_SOFT_PREFIX = re.compile(
    r"^(?:"
    r"c[oó]pia\s+de\s+|"
    r"copia\s+de\s+|"
    r"cancelad[oa]s?\s+|"
    r"cancell?ed\s+"
    r")+",
    re.IGNORECASE,
)

_DDMM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 0101, 010126, 01-01, 01/01, 01 01, 01.01
    re.compile(
        r"^(\d{1,2})\s*[-/\.\s]\s*(\d{1,2})\b"
    ),
    re.compile(r"^(\d{2})(\d{2})(?:\d{2})?\b"),
)


@dataclass(frozen=True)
class FilenamePeriod:
    """Approximate calendar window encoded in a manifesto filename."""

    year: int
    start_month: int
    end_month: int

    @property
    def midpoint(self) -> date:
        if self.start_month <= self.end_month:
            mid_m = (self.start_month + self.end_month) // 2
            return date(self.year, mid_m, 15)
        # Wrap across year (rare): midpoint near Dec/Jan
        return date(self.year, 12, 15)


@dataclass
class RepairReport:
    scanned: int = 0
    dates_fixed: int = 0
    cancelled_removed: int = 0
    fingerprint_collisions: int = 0
    unchanged: int = 0
    dry_run: bool = False
    samples: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "dates_fixed": self.dates_fixed,
            "cancelled_removed": self.cancelled_removed,
            "fingerprint_collisions": self.fingerprint_collisions,
            "unchanged": self.unchanged,
            "dry_run": self.dry_run,
            "samples": self.samples or [],
        }


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


def strip_sheet_prefix(sheet_name: str) -> str:
    return _SHEET_SOFT_PREFIX.sub("", sheet_name.strip()).strip()


def is_skippable_sheet(sheet_name: str) -> bool:
    """True for templates and cancelled flight tabs.

    Matches Cancelado as prefix, suffix, or parenthetical — including truncated
    Excel tab names like ``… CANCELAD`` / ``… CANCELA``.
    """
    folded = _fold(sheet_name)
    if any(folded.startswith(p) for p in SKIP_SHEET_PREFIXES):
        return True
    # "Cancelado 0101…", "0108 … (CANCELADO)", "… CANCELADO !", "… CANCELAD"
    return bool(re.search(r"\bcancel", folded))


def extract_sheet_ddmm(sheet_name: str) -> Optional[tuple[int, int]]:
    """Extract (day, month) from a manifesto sheet title."""
    cleaned = strip_sheet_prefix(sheet_name)
    if not cleaned:
        return None
    for pattern in _DDMM_PATTERNS:
        m = pattern.match(cleaned)
        if not m:
            continue
        dd, mm = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return dd, mm
    return None


def year_hint_from_filename(filename: str) -> Optional[int]:
    period = parse_filename_period(filename)
    if period:
        return period.year
    m = re.search(r"(20\d{2})", filename)
    if m:
        return int(m.group(1))
    # Compact forms: Out24, Nov25
    m = re.search(r"(?:^|[^0-9])(\d{2})(?:\D|$)", filename)
    if m:
        yy = int(m.group(1))
        if 20 <= yy <= 39:
            return 2000 + yy
    return None


def parse_filename_period(filename: str) -> Optional[FilenamePeriod]:
    """Parse period hints like Nov-Dez_2025, Jan-Abr_2025, Out24, MaiJun2026."""
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    folded = _fold(stem)
    # Normalize separators
    folded = folded.replace("_", " ").replace(".", " ")

    year: Optional[int] = None
    ym = re.search(r"(20\d{2})", folded)
    if ym:
        year = int(ym.group(1))
    else:
        ym2 = re.search(r"(?:^|[^0-9])(\d{2})(?:\D|$)", folded)
        if ym2:
            yy = int(ym2.group(1))
            if 20 <= yy <= 39:
                year = 2000 + yy
    if year is None:
        return None

    # Collect month tokens in order of appearance
    tokens = re.findall(
        r"jan(?:eiro)?|fev(?:ereiro)?|mar(?:[cç]o)?|abr(?:il)?|"
        r"mai(?:o)?|jun(?:ho)?|jul(?:ho)?|ago(?:sto)?|"
        r"set(?:embro)?|out(?:ubro)?|nov(?:embro)?|dez(?:embro)?",
        folded,
        flags=re.IGNORECASE,
    )
    months: list[int] = []
    for tok in tokens:
        key = _fold(tok)
        # Normalize març/marco variants already folded to marco/marc?
        if key.startswith("mar"):
            months.append(3)
        elif key in _MONTH_PT:
            months.append(_MONTH_PT[key])
        else:
            # Prefix match (jan, fev…)
            for name, num in _MONTH_PT.items():
                if key.startswith(name[:3]):
                    months.append(num)
                    break

    if not months:
        return FilenamePeriod(year=year, start_month=1, end_month=12)

    start_m = months[0]
    end_m = months[-1] if len(months) > 1 else months[0]
    return FilenamePeriod(year=year, start_month=start_m, end_month=end_m)


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _candidate_years(period: FilenamePeriod, month: int) -> list[int]:
    """Years where (month) could plausibly fall relative to the file period."""
    y = period.year
    years = [y, y - 1, y + 1]
    # Prefer order: in-period year first
    if period.start_month <= period.end_month:
        if period.start_month <= month <= period.end_month:
            years = [y, y - 1, y + 1]
        elif month < period.start_month:
            # Early month after a late-year file → next year
            # e.g. Nov-Dez_2025 + Jan → 2026
            years = [y + 1, y, y - 1]
        else:
            # Late month before an early-year file → previous year
            # e.g. Jan-Abr_2025 + Dec → 2024
            years = [y - 1, y, y + 1]
    else:
        # Wrapped period inside filename year / year+1
        years = [y, y + 1, y - 1]
    # Unique preserve order
    seen: set[int] = set()
    ordered: list[int] = []
    for cand in years:
        if cand not in seen:
            seen.add(cand)
            ordered.append(cand)
    return ordered


def _best_date_for_ddmm(
    dd: int, mm: int, filename: str, cell_date: Optional[date] = None
) -> Optional[date]:
    period = parse_filename_period(filename)
    if period is None:
        yh = year_hint_from_filename(filename)
        if yh is None:
            return cell_date
        period = FilenamePeriod(year=yh, start_month=1, end_month=12)

    candidates: list[date] = []
    for y in _candidate_years(period, mm):
        d = _safe_date(y, mm, dd)
        if d:
            candidates.append(d)
    if not candidates:
        return cell_date

    # If cell day/month matches, snap its year to the best candidate
    if cell_date and (cell_date.day, cell_date.month) == (dd, mm):
        mid = period.midpoint
        return min(candidates, key=lambda d: abs((d - mid).days))

    if cell_date and abs(cell_date.year - period.year) <= 1:
        # Trust cell when it already sits near the period and day/month disagree
        # only slightly (parser noise) — still prefer sheet DDMM near midpoint
        mid = period.midpoint
        best = min(candidates, key=lambda d: abs((d - mid).days))
        # Keep cell if it is closer to midpoint than any candidate swap would be
        # and day/month already match a valid calendar reading of the cell itself
        if abs((cell_date - mid).days) <= abs((best - mid).days):
            return cell_date
        return best

    mid = period.midpoint
    return min(candidates, key=lambda d: abs((d - mid).days))


def resolve_flight_date(
    cell_date: Any, sheet_name: str, filename: str
) -> Optional[date]:
    """Resolve the operational flight date from cell + sheet + filename hints."""
    fd: Optional[date] = None
    if isinstance(cell_date, datetime):
        fd = cell_date.date()
    elif isinstance(cell_date, date):
        fd = cell_date

    ddmm = extract_sheet_ddmm(sheet_name)
    if not ddmm:
        return fd

    dd, mm = ddmm
    return _best_date_for_ddmm(dd, mm, filename, cell_date=fd)


def repair_existing_flights(
    db: Session,
    *,
    fix_null_dates: bool = True,
    fix_inconsistent_dates: bool = True,
    remove_cancelled: bool = True,
    dry_run: bool = False,
    sample_limit: int = 25,
) -> RepairReport:
    """Fix dates / drop cancelled flights already stored in the database."""
    # Local import avoids circular dependency at module load
    from app.models import Boarding, Flight, Passenger
    from app.parser import flight_fingerprint
    from app.parser import ParsedFlight, ParsedPassenger

    report = RepairReport(dry_run=dry_run, samples=[])
    flights = list(db.scalars(select(Flight)).all())
    report.scanned = len(flights)

    for fl in flights:
        if remove_cancelled and is_skippable_sheet(fl.sheet_name):
            report.cancelled_removed += 1
            if report.samples is not None and len(report.samples) < sample_limit:
                report.samples.append(
                    {
                        "action": "remove_cancelled",
                        "flight_id": fl.id,
                        "sheet_name": fl.sheet_name,
                        "source_file": fl.source_file,
                        "old_date": fl.flight_date.isoformat() if fl.flight_date else None,
                    }
                )
            if not dry_run:
                # Adjust passenger boarding counts before cascade delete
                pax_ids = list(
                    db.scalars(
                        select(Boarding.passenger_id).where(Boarding.flight_id == fl.id)
                    ).all()
                )
                db.delete(fl)
                db.flush()
                for pid in pax_ids:
                    pax = db.get(Passenger, pid)
                    if not pax:
                        continue
                    remaining = list(
                        db.scalars(
                            select(Boarding).where(Boarding.passenger_id == pid)
                        ).all()
                    )
                    pax.total_boardings = len(remaining)
                    dates = [b.flight_date for b in remaining if b.flight_date]
                    pax.first_seen = min(dates) if dates else None
                    pax.last_seen = max(dates) if dates else None
            continue

        if not fix_null_dates and not fix_inconsistent_dates:
            report.unchanged += 1
            continue

        ddmm = extract_sheet_ddmm(fl.sheet_name)
        if not ddmm:
            report.unchanged += 1
            continue

        new_date = resolve_flight_date(fl.flight_date, fl.sheet_name, fl.source_file)
        if new_date is None:
            report.unchanged += 1
            continue

        needs_fix = False
        if fix_null_dates and fl.flight_date is None:
            needs_fix = True
        elif fix_inconsistent_dates and fl.flight_date != new_date:
            # Only rewrite when sheet DDMM disagrees with stored day/month
            # or year is more than 1 away from filename hint
            dd, mm = ddmm
            stored = fl.flight_date
            yh = year_hint_from_filename(fl.source_file)
            if (stored.day, stored.month) != (dd, mm):
                needs_fix = True
            elif yh is not None and abs(stored.year - yh) > 1:
                needs_fix = True
            elif stored != new_date and abs((stored - new_date).days) >= 28:
                needs_fix = True

        if not needs_fix:
            report.unchanged += 1
            continue

        # Build a lightweight ParsedFlight to recompute fingerprint
        boardings = list(
            db.scalars(select(Boarding).where(Boarding.flight_id == fl.id)).all()
        )
        parsed = ParsedFlight(
            sheet_name=fl.sheet_name,
            flight_date=new_date,
            flight_time=fl.flight_time,
            origin=fl.origin,
            destination=fl.destination,
            origin_code=fl.origin_code,
            dest_code=fl.dest_code,
            aircraft_reg=fl.aircraft_reg,
            aircraft_code=fl.aircraft_code,
            passengers=[
                ParsedPassenger(
                    name=b.passenger_name_raw,
                    document=b.document_raw,
                    identity_key=f"boarding:{b.id}",
                    document_normalized=None,
                )
                for b in boardings
            ],
        )
        # Prefer real identity keys from passenger rows for stable fingerprints
        for i, b in enumerate(boardings):
            pax = db.get(Passenger, b.passenger_id)
            if pax:
                parsed.passengers[i] = ParsedPassenger(
                    name=b.passenger_name_raw,
                    document=b.document_raw,
                    identity_key=pax.identity_key,
                    document_normalized=pax.document_normalized,
                )
        new_fp = flight_fingerprint(parsed, fl.source_file)

        if new_fp != fl.fingerprint:
            clash = db.scalar(
                select(Flight.id).where(
                    Flight.fingerprint == new_fp, Flight.id != fl.id
                )
            )
            if clash:
                report.fingerprint_collisions += 1
                if report.samples is not None and len(report.samples) < sample_limit:
                    report.samples.append(
                        {
                            "action": "fingerprint_collision",
                            "flight_id": fl.id,
                            "clash_flight_id": clash,
                            "sheet_name": fl.sheet_name,
                            "source_file": fl.source_file,
                            "old_date": fl.flight_date.isoformat()
                            if fl.flight_date
                            else None,
                            "new_date": new_date.isoformat(),
                        }
                    )
                continue

        report.dates_fixed += 1
        if report.samples is not None and len(report.samples) < sample_limit:
            report.samples.append(
                {
                    "action": "fix_date",
                    "flight_id": fl.id,
                    "sheet_name": fl.sheet_name,
                    "source_file": fl.source_file,
                    "old_date": fl.flight_date.isoformat() if fl.flight_date else None,
                    "new_date": new_date.isoformat(),
                }
            )
        if not dry_run:
            fl.flight_date = new_date
            fl.fingerprint = new_fp
            for b in boardings:
                b.flight_date = new_date
            db.flush()
            # Refresh passenger first/last seen for people on this flight
            for b in boardings:
                pax = db.get(Passenger, b.passenger_id)
                if not pax:
                    continue
                dates = [
                    d
                    for d in db.scalars(
                        select(Boarding.flight_date).where(
                            Boarding.passenger_id == pax.id,
                            Boarding.flight_date.is_not(None),
                        )
                    ).all()
                    if d is not None
                ]
                if dates:
                    pax.first_seen = min(dates)
                    pax.last_seen = max(dates)

    if not dry_run:
        db.commit()
    else:
        db.rollback()
    return report


def annotate_gap_hint(filename: str, last_date: Optional[date]) -> Optional[str]:
    """Optional note when a bi-month file looks truncated vs its period."""
    period = parse_filename_period(filename)
    if not period or last_date is None:
        return None
    # If period ends in month M but last boarding is before mid of M-1, warn
    end = _safe_date(period.year, period.end_month, 28) or date(
        period.year, period.end_month, 1
    )
    if period.start_month > period.end_month:
        end = _safe_date(period.year + 1, period.end_month, 28) or end
    if last_date < end - timedelta(days=20):
        return (
            f"Possible incomplete period in {filename}: "
            f"last date {last_date.isoformat()} before expected end ~{end.isoformat()}"
        )
    return None
