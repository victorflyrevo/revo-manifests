"""Parse REVO manifesto Excel workbooks (one sheet = one flight)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO
from typing import Any, BinaryIO, Optional

import openpyxl

SKIP_PREFIXES = ("base de dados", "xxxx")


@dataclass
class ParsedPassenger:
    name: str
    document: Optional[str]
    identity_key: str
    document_normalized: Optional[str]


@dataclass
class ParsedFlight:
    sheet_name: str
    flight_date: Optional[date]
    flight_time: Optional[str]
    origin: Optional[str]
    destination: Optional[str]
    origin_code: Optional[str]
    dest_code: Optional[str]
    aircraft_reg: Optional[str]
    aircraft_code: Optional[str]
    passengers: list[ParsedPassenger] = field(default_factory=list)
    fingerprint: str = ""


@dataclass
class ParseResult:
    source_file: str
    flights: list[ParsedFlight]
    skipped_sheets: int = 0


def norm_name(s: Any) -> Optional[str]:
    if s is None:
        return None
    text = re.sub(r"\s+", " ", str(s).strip())
    if not text:
        return None
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.upper()


def norm_doc(s: Any) -> Optional[str]:
    if s is None:
        return None
    text = str(s).strip()
    if not text or text.lower() in {
        "nan",
        "none",
        "à confirmar",
        "a confirmar",
        "confirmar",
    }:
        return None
    digits = re.sub(r"[^0-9A-Za-z]", "", text).upper()
    return digits if len(digits) >= 4 else None


def airport_code(s: Any) -> Optional[str]:
    if s is None:
        return None
    text = str(s).strip()
    if not text:
        return None
    m = re.match(r"^([A-Z0-9]{3,4})\b", text.upper())
    return m.group(1) if m else text[:32]


def identity_key(name: str, document: Any) -> tuple[str, Optional[str]]:
    doc = norm_doc(document)
    n = norm_name(name)
    if doc:
        return f"doc:{doc}", doc
    if n:
        return f"name:{n}", None
    return f"raw:{hashlib.sha1(name.encode()).hexdigest()[:16]}", None


def sheet_ddmm(sn: str) -> Optional[tuple[int, int]]:
    m = re.match(r"^(?:C[oó]pia de\s+)?(\d{4})\b", sn.strip(), re.I)
    if not m:
        return None
    dd, mm = int(m.group(1)[:2]), int(m.group(1)[2:])
    if 1 <= mm <= 12 and 1 <= dd <= 31:
        return dd, mm
    return None


def year_hint_from_filename(filename: str) -> Optional[int]:
    m = re.search(r"(20\d{2})", filename)
    return int(m.group(1)) if m else None


def resolve_date(
    cell_date: Any, sheet_name: str, filename: str
) -> Optional[date]:
    fd: Optional[date] = None
    if isinstance(cell_date, datetime):
        fd = cell_date.date()
    elif isinstance(cell_date, date):
        fd = cell_date

    yh = year_hint_from_filename(filename)
    ddmm = sheet_ddmm(sheet_name)
    if not ddmm or not yh:
        return fd

    dd, mm = ddmm
    years = [yh]
    if "Jan" in filename:
        years.append(yh - 1)

    candidates: list[date] = []
    for y in years:
        try:
            candidates.append(date(y, mm, dd))
        except ValueError:
            pass

    if fd and candidates:
        if (fd.day, fd.month) == (dd, mm) and abs(fd.year - yh) <= 1:
            return fd
        mid = date(yh, 6, 15)
        return min(candidates, key=lambda d: abs((d - mid).days))
    if candidates and not fd:
        mid = date(yh, 6, 15)
        return min(candidates, key=lambda d: abs((d - mid).days))
    return fd


def flight_fingerprint(fl: ParsedFlight, source_file: str) -> str:
    pax = "|".join(sorted(p.identity_key for p in fl.passengers))
    raw = "|".join(
        [
            fl.flight_date.isoformat() if fl.flight_date else "",
            fl.origin_code or "",
            fl.dest_code or "",
            fl.flight_time or "",
            fl.aircraft_code or fl.aircraft_reg or "",
            pax,
            # keep sheet canon lightly to distinguish parallel empty flights
            re.sub(r"^C[oó]pia de\s+", "", fl.sheet_name.strip(), flags=re.I),
            source_file,
        ]
    )
    # Cross-file dedup: same operational identity without source_file
    operational = "|".join(
        [
            fl.flight_date.isoformat() if fl.flight_date else "",
            fl.origin_code or "",
            fl.dest_code or "",
            fl.flight_time or "",
            fl.aircraft_code or fl.aircraft_reg or "",
            pax,
        ]
    )
    return hashlib.sha256(operational.encode("utf-8")).hexdigest()


def _parse_sheet(ws: Any, sn: str, filename: str) -> ParsedFlight:
    """Parse one sheet. Reads all rows until TOTAL — no sheet/passenger cap."""
    flight_date = origin = dest = hora = matricula = None
    passengers: list[ParsedPassenger] = []
    pax_mode = False
    empty_pax_streak = 0

    # No max_row: process every row on the sheet
    for row in ws.iter_rows(min_row=1, max_col=16, values_only=True):
        if not row:
            if pax_mode:
                empty_pax_streak += 1
                if empty_pax_streak >= 5:
                    break
            continue

        if any(isinstance(v, str) and "TOTAL" in v.upper() for v in row if v is not None):
            break

        label = row[0]
        if not pax_mode and label is not None:
            lab = str(label).strip().lower()
            val = row[3] if len(row) > 3 else None
            if lab.startswith("data"):
                flight_date = val
            elif lab.startswith("hora"):
                hora = val
            elif lab.startswith("origem"):
                origin = val
            elif lab.startswith("destino"):
                dest = val
            elif "matr" in lab:
                matricula = row[13] if len(row) > 13 else val
            elif label == "#" or (
                isinstance(row[1], str) and "nome" in str(row[1]).lower()
            ):
                pax_mode = True
            continue

        if not pax_mode:
            # Header row detected by column B containing "Nome Passageiro"
            if isinstance(row[1], str) and "nome" in row[1].lower():
                pax_mode = True
            continue

        name = row[1] if len(row) > 1 else None
        doc = row[7] if len(row) > 7 else None
        if name is None or str(name).strip() == "":
            empty_pax_streak += 1
            if empty_pax_streak >= 5:
                break
            continue

        empty_pax_streak = 0
        name_s = str(name).strip()
        key, doc_n = identity_key(name_s, doc)
        passengers.append(
            ParsedPassenger(
                name=name_s,
                document=str(doc).strip() if doc is not None else None,
                identity_key=key,
                document_normalized=doc_n,
            )
        )

    fd = resolve_date(flight_date, sn, filename)

    hora_s = None
    if isinstance(hora, datetime):
        hora_s = hora.strftime("%H:%M")
    elif hasattr(hora, "hour"):
        hora_s = f"{hora.hour:02d}:{hora.minute:02d}"
    elif hora is not None:
        hora_s = str(hora)[:16]

    ac = None
    m = re.search(r"\b(OOE\d*|OMB\d*|OMH\d*)\b", sn.upper())
    if m:
        ac = m.group(1)

    fl = ParsedFlight(
        sheet_name=sn,
        flight_date=fd,
        flight_time=hora_s,
        origin=str(origin).strip() if origin else None,
        destination=str(dest).strip() if dest else None,
        origin_code=airport_code(origin),
        dest_code=airport_code(dest),
        aircraft_reg=str(matricula).strip() if matricula else None,
        aircraft_code=ac,
        passengers=passengers,
    )
    fl.fingerprint = flight_fingerprint(fl, filename)
    return fl


def parse_workbook(file_obj: BinaryIO, filename: str) -> ParseResult:
    """Parse every sheet in the workbook — no sheet-count limit."""
    wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
    flights: list[ParsedFlight] = []
    skipped = 0

    for sn in wb.sheetnames:
        sl = sn.strip().lower()
        if sl.startswith(SKIP_PREFIXES):
            skipped += 1
            continue
        flights.append(_parse_sheet(wb[sn], sn, filename))

    wb.close()
    return ParseResult(source_file=filename, flights=flights, skipped_sheets=skipped)


def parse_bytes(data: bytes, filename: str) -> ParseResult:
    return parse_workbook(BytesIO(data), filename)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
