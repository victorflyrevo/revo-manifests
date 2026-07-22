"""Unit tests for manifesto date / sheet correction helpers."""

from __future__ import annotations

from datetime import date

from app.corrections import (
    extract_sheet_ddmm,
    is_excluded_loop_flight,
    is_skippable_sheet,
    parse_filename_period,
    resolve_flight_date,
)


def test_skip_cancelled_and_template_sheets() -> None:
    assert is_skippable_sheet("Cancelado 0101 OOE1")
    assert is_skippable_sheet("CANCELADA 1503 OMB2")
    assert is_skippable_sheet("Cópia de Cancelado 0101 OOE1")
    assert is_skippable_sheet("0108 SBSPxSJJY OMB1 (CANCELADO)")
    assert is_skippable_sheet("0202 SIIRxSBGR OMB - CANCELADO !")
    assert is_skippable_sheet("0103 SDMN x SBGR OMB - CANCELAD")  # truncated
    assert is_skippable_sheet("0503 SBGR x SDXQ OOE CANCELADO")
    assert is_skippable_sheet("Base de Dados")
    assert is_skippable_sheet("XXXX template")
    assert not is_skippable_sheet("0101 OOE1 SSJ-SJK")
    assert not is_skippable_sheet("Cópia de 1506 OMB2")


def test_extract_sheet_ddmm_variants() -> None:
    assert extract_sheet_ddmm("0101 OOE1") == (1, 1)
    assert extract_sheet_ddmm("Cópia de 1506 OMB2") == (15, 6)
    assert extract_sheet_ddmm("Cancelado 0101 OOE1") == (1, 1)
    assert extract_sheet_ddmm("01-01 OOE1") == (1, 1)
    assert extract_sheet_ddmm("01/06 OMB") == (1, 6)
    assert extract_sheet_ddmm("01 06 OOE") == (1, 6)
    assert extract_sheet_ddmm("31.12 OOE") == (31, 12)
    assert extract_sheet_ddmm("Sem data") is None


def test_parse_filename_period() -> None:
    p = parse_filename_period("Nov-Dez_2025.xlsx")
    assert p is not None
    assert p.year == 2025
    assert p.start_month == 11
    assert p.end_month == 12

    p = parse_filename_period("Jan-Abr_2025.xlsx")
    assert p is not None
    assert (p.start_month, p.end_month, p.year) == (1, 4, 2025)

    p = parse_filename_period("Out24.xlsx")
    assert p is not None
    assert p.year == 2024
    assert p.start_month == 10

    p = parse_filename_period("Mai-Jun_2026.ods")
    assert p is not None
    assert (p.start_month, p.end_month, p.year) == (5, 6, 2026)


def test_year_boundary_nov_dez_to_january() -> None:
    # Sheet in January inside a Nov-Dez_2025 package → 2026-01-01
    assert resolve_flight_date(None, "0101 OOE1", "Nov-Dez_2025.xlsx") == date(
        2026, 1, 1
    )
    assert resolve_flight_date(None, "1512 OOE1", "Nov-Dez_2025.xlsx") == date(
        2025, 12, 15
    )


def test_year_boundary_jan_abr_december_prev_year() -> None:
    # Late December sheet wrongly sitting in an early-year file → previous year
    assert resolve_flight_date(None, "2012 OOE1", "Jan-Abr_2025.xlsx") == date(
        2024, 12, 20
    )
    assert resolve_flight_date(None, "1503 OOE1", "Jan-Abr_2025.xlsx") == date(
        2025, 3, 15
    )


def test_resolve_prefers_sheet_ddmm_when_cell_missing() -> None:
    assert resolve_flight_date(None, "2207 OMB2", "Jul-Ago_2025.xlsx") == date(
        2025, 7, 22
    )


def test_resolve_keeps_matching_cell_near_period() -> None:
    cell = date(2025, 7, 22)
    assert resolve_flight_date(cell, "2207 OMB2", "Jul-Ago_2025.xlsx") == cell


def test_resolve_corrects_wrong_cell_year_on_boundary() -> None:
    # Cell says 2025-01-01 but file is Nov-Dez_2025 → snap to 2026
    cell = date(2025, 1, 1)
    assert resolve_flight_date(cell, "0101 OOE1", "Nov-Dez_2025.xlsx") == date(
        2026, 1, 1
    )


def test_sheet_ddmm_3006_means_30_june() -> None:
    assert extract_sheet_ddmm("3006 SIIRxSBGR OMB 2") == (30, 6)
    assert resolve_flight_date(None, "3006 SIIRxSBGR OMB 2", "Mai-Jun_2026.xlsx") == date(
        2026, 6, 30
    )


def test_sheet_ddmm_wins_over_conflicting_cell_month() -> None:
    # Cell wrongly says 30/05; tab 3006 must become 30/06
    cell = date(2026, 5, 30)
    assert resolve_flight_date(
        cell, "3006 SIIRxSBGR OMB 2", "Manifesto REVO Mai-Jun_2026.xlsx"
    ) == date(2026, 6, 30)


def test_cell_literal_3006_parsed_as_30_june() -> None:
    assert resolve_flight_date(3006, "Voo OMB", "Mai-Jun_2026.xlsx") == date(2026, 6, 30)
    assert resolve_flight_date("3006", "Voo OMB", "Mai-Jun_2025.xlsx") == date(
        2025, 6, 30
    )


def test_siav_loop_with_passengers_excluded() -> None:
    assert is_excluded_loop_flight("SIAV", "SIAV", passenger_count=3) is True
    assert is_excluded_loop_flight("siav", "siav", passenger_count=1) is True
    # Empty / no pax — not the training rule
    assert is_excluded_loop_flight("SIAV", "SIAV", passenger_count=0) is False
    assert is_excluded_loop_flight("SIAV", "SBGR", passenger_count=3) is False
    assert is_excluded_loop_flight("SBGR", "SIAV", passenger_count=3) is False
