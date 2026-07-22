"""Tests for airport/helipad code extraction (VARCHAR(8) safe)."""

from __future__ import annotations

from app.parser import AIRPORT_CODE_MAX, airport_code


def test_standard_icao_and_local_codes() -> None:
    assert airport_code("SBSP") == "SBSP"
    assert airport_code("SDXQ - International Plaza") == "SDXQ"
    assert airport_code("ZCAPS - Joaquim Egídio") == "ZCAPS"
    assert airport_code("SJK") == "SJK"


def test_never_exceeds_column_width() -> None:
    assert airport_code("ZCAPS - Joaquim Egídio") is not None
    assert len(airport_code("ZCAPS - Joaquim Egídio") or "") <= AIRPORT_CODE_MAX
    # Pathological: no short token — still capped / rejected safely
    long_junk = "ABCDEFGHIJKLMNOP - Somewhere"
    code = airport_code(long_junk)
    assert code == "ABCDEFGH"
    assert len(code) <= AIRPORT_CODE_MAX


def test_empty_and_garbage() -> None:
    assert airport_code(None) is None
    assert airport_code("") is None
    assert airport_code("  ") is None
    assert airport_code("AB") is None
