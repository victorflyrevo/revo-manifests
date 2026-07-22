"""Mission cut aligned with Sigtrip-style operational counting.

A manifesto *leg* is one sheet / takeoff. A *mission* chains connected legs
on the same aircraft and calendar day (dest of leg N → origin of leg N+1).

This matches Sigtrip ~2000 on a trailing-12-month window (~2006 in audit),
versus ~2500 raw legs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Optional


_AC_RE = re.compile(r"\b((?:OOE|OMB|OMH)\d*)\b", re.I)
_TIME_RE = re.compile(r"^(\d{1,2}):?(\d{2})?")


def aircraft_token(sheet_name: str | None, aircraft_reg: str | None = None, aircraft_code: str | None = None) -> Optional[str]:
    """Best aircraft identity for mission grouping."""
    for raw in (aircraft_reg, aircraft_code, sheet_name):
        if not raw:
            continue
        m = _AC_RE.search(str(raw))
        if m:
            return m.group(1).upper()
    return None


def minutes_of_day(flight_time: str | None) -> Optional[int]:
    if not flight_time:
        return None
    text = str(flight_time).strip().lower().replace("h", ":")
    m = _TIME_RE.match(text)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    if hh > 23 or mm > 59:
        return None
    return hh * 60 + mm


@dataclass(frozen=True)
class MissionLeg:
    flight_id: int
    flight_date: date
    flight_time: Optional[str]
    origin_code: Optional[str]
    dest_code: Optional[str]
    sheet_name: Optional[str] = None
    aircraft_reg: Optional[str] = None
    aircraft_code: Optional[str] = None

    @property
    def origin(self) -> str:
        return (self.origin_code or "").strip().upper()

    @property
    def dest(self) -> str:
        return (self.dest_code or "").strip().upper()

    @property
    def aircraft(self) -> Optional[str]:
        return aircraft_token(self.sheet_name, self.aircraft_reg, self.aircraft_code)


@dataclass
class Mission:
    mission_id: str
    flight_date: date
    aircraft: Optional[str]
    flight_ids: list[int]
    legs: int

    @property
    def month(self) -> str:
        return self.flight_date.strftime("%Y-%m")


def assign_missions(legs: Iterable[MissionLeg]) -> list[Mission]:
    """Group legs into Sigtrip-style missions (connected same-day chains)."""
    by_day_ac: dict[tuple[date, str], list[MissionLeg]] = defaultdict(list)
    orphans: list[MissionLeg] = []

    for leg in legs:
        if not leg.flight_date:
            continue
        ac = leg.aircraft
        if not ac or not leg.origin or not leg.dest:
            orphans.append(leg)
            continue
        by_day_ac[(leg.flight_date, ac)].append(leg)

    missions: list[Mission] = []

    for leg in orphans:
        missions.append(
            Mission(
                mission_id=f"leg:{leg.flight_id}",
                flight_date=leg.flight_date,
                aircraft=leg.aircraft,
                flight_ids=[leg.flight_id],
                legs=1,
            )
        )

    for (day, ac), group in by_day_ac.items():
        ordered = sorted(
            group,
            key=lambda x: (
                minutes_of_day(x.flight_time) is None,
                minutes_of_day(x.flight_time) or 0,
                x.flight_id,
            ),
        )
        used = [False] * len(ordered)
        chain_idx = 0
        for i, start in enumerate(ordered):
            if used[i]:
                continue
            chain = [start]
            used[i] = True
            cur = start
            changed = True
            while changed:
                changed = False
                for j, nxt in enumerate(ordered):
                    if used[j]:
                        continue
                    t_cur = minutes_of_day(cur.flight_time)
                    t_nxt = minutes_of_day(nxt.flight_time)
                    if t_cur is not None and t_nxt is not None and t_nxt < t_cur:
                        continue
                    if nxt.origin == cur.dest:
                        used[j] = True
                        chain.append(nxt)
                        cur = nxt
                        changed = True
                        break
            chain_idx += 1
            missions.append(
                Mission(
                    mission_id=f"{day.isoformat()}:{ac}:{chain_idx}",
                    flight_date=day,
                    aircraft=ac,
                    flight_ids=[x.flight_id for x in chain],
                    legs=len(chain),
                )
            )

    return missions


def count_missions(
    legs: Iterable[MissionLeg],
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> int:
    missions = assign_missions(legs)
    n = 0
    for m in missions:
        if start and m.flight_date < start:
            continue
        if end and m.flight_date > end:
            continue
        n += 1
    return n


def missions_by_month(legs: Iterable[MissionLeg]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for m in assign_missions(legs):
        out[m.month] += 1
    return dict(out)


def legs_from_flight_rows(rows: Iterable[Any]) -> list[MissionLeg]:
    """Build legs from Flight ORM rows or mapping-like objects."""
    legs: list[MissionLeg] = []
    for fl in rows:
        fd = getattr(fl, "flight_date", None)
        if fd is None and isinstance(fl, dict):
            raw = fl.get("flight_date")
            fd = date.fromisoformat(str(raw)[:10]) if raw else None
        if fd is None:
            continue
        if isinstance(fl, dict):
            legs.append(
                MissionLeg(
                    flight_id=int(fl["id"]),
                    flight_date=fd,
                    flight_time=fl.get("flight_time"),
                    origin_code=fl.get("origin_code"),
                    dest_code=fl.get("dest_code"),
                    sheet_name=fl.get("sheet_name"),
                    aircraft_reg=fl.get("aircraft_reg"),
                    aircraft_code=fl.get("aircraft_code"),
                )
            )
        else:
            legs.append(
                MissionLeg(
                    flight_id=int(fl.id),
                    flight_date=fd,
                    flight_time=fl.flight_time,
                    origin_code=fl.origin_code,
                    dest_code=fl.dest_code,
                    sheet_name=fl.sheet_name,
                    aircraft_reg=fl.aircraft_reg,
                    aircraft_code=fl.aircraft_code,
                )
            )
    return legs
