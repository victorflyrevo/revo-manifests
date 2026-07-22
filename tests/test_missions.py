"""Tests for Sigtrip-style mission cut."""

from __future__ import annotations

from datetime import date

from app.missions import MissionLeg, assign_missions, count_missions, missions_by_month


def test_connected_legs_same_aircraft_one_mission() -> None:
    legs = [
        MissionLeg(
            flight_id=1,
            flight_date=date(2025, 11, 9),
            flight_time="09:00",
            origin_code="SWYD",
            dest_code="SSXK",
            sheet_name="0911 SWYD x SSXK OMB 1",
        ),
        MissionLeg(
            flight_id=2,
            flight_date=date(2025, 11, 9),
            flight_time="17:20",
            origin_code="SSXK",
            dest_code="SWYD",
            sheet_name="0911 SSXK x SWYD OMB 7",
        ),
    ]
    missions = assign_missions(legs)
    assert len(missions) == 1
    assert missions[0].legs == 2


def test_parallel_aircraft_are_separate_missions() -> None:
    legs = [
        MissionLeg(
            flight_id=1,
            flight_date=date(2025, 11, 9),
            flight_time="09:30",
            origin_code="SDXQ",
            dest_code="SSXK",
            sheet_name="0911 SDXQ x SSXK OOE 1",
        ),
        MissionLeg(
            flight_id=2,
            flight_date=date(2025, 11, 9),
            flight_time="10:00",
            origin_code="SDMN",
            dest_code="SSXK",
            sheet_name="0911 SDMN x SSXK OMH 1",
        ),
    ]
    assert count_missions(legs) == 2


def test_disconnected_same_aircraft_two_missions() -> None:
    legs = [
        MissionLeg(
            flight_id=1,
            flight_date=date(2025, 5, 1),
            flight_time="08:00",
            origin_code="SBGR",
            dest_code="SDXQ",
            sheet_name="0105 SBGRxSDXQ OOE",
        ),
        MissionLeg(
            flight_id=2,
            flight_date=date(2025, 5, 1),
            flight_time="15:00",
            origin_code="SBGR",
            dest_code="SIIR",
            sheet_name="0105 SBGRxSIIR OOE",
        ),
    ]
    assert count_missions(legs) == 2


def test_missions_by_month() -> None:
    legs = [
        MissionLeg(
            flight_id=1,
            flight_date=date(2025, 1, 2),
            flight_time="10:00",
            origin_code="A",
            dest_code="B",
            sheet_name="OOE",
        ),
        MissionLeg(
            flight_id=2,
            flight_date=date(2025, 2, 2),
            flight_time="10:00",
            origin_code="A",
            dest_code="B",
            sheet_name="OMB",
        ),
    ]
    assert missions_by_month(legs) == {"2025-01": 1, "2025-02": 1}
