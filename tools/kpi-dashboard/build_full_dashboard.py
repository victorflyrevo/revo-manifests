#!/usr/bin/env python3
"""Build local dashboard with longest history + MoM / YoY growth charts.

Reads boardings from Manifests API export (or local CSV) — outside git.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen

OUT = Path(__file__).resolve().parent

# Reuse API mission cut (Sigtrip-style connected chains)
_REPO_ROOT = OUT.parents[1]  # tools/kpi-dashboard → repo root
for candidate in (_REPO_ROOT, Path("/workspace")):
    if (candidate / "app" / "missions.py").exists():
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        break
from app.missions import MissionLeg, assign_missions, missions_by_month  # noqa: E402

# Match Cancelado / CANCELADO / CANCELAD (truncated) anywhere in the sheet tab
_CANCELLED_SHEET = re.compile(r"\bcancel", re.I)


def is_cancelled_sheet(sheet_name: str) -> bool:
    return bool(_CANCELLED_SHEET.search(sheet_name or ""))


def is_siav_loop(row: dict) -> bool:
    o = str(row.get("origin_code") or "").strip().upper()
    d = str(row.get("dest_code") or "").strip().upper()
    return o == "SIAV" and d == "SIAV"


def drop_cancelled_rows(rows: list[dict]) -> list[dict]:
    kept = []
    removed_cancel = removed_siav = 0
    for r in rows:
        if is_cancelled_sheet(str(r.get("sheet_name") or "")):
            removed_cancel += 1
            continue
        if is_siav_loop(r):
            removed_siav += 1
            continue
        kept.append(r)
    if removed_cancel:
        print(f"Dropped {removed_cancel} cancelled-sheet boarding row(s)")
    if removed_siav:
        print(f"Dropped {removed_siav} SIAV→SIAV training boarding row(s)")
    return kept


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def month_end(d: date) -> date:
    return add_months(month_start(d), 1) - timedelta(days=1)


def month_label(d: date) -> str:
    return d.strftime("%Y-%m")


def iter_months(start: date, end: date) -> list[date]:
    cur, last = month_start(start), month_start(end)
    out = []
    while cur <= last:
        out.append(cur)
        cur = add_months(cur, 1)
    return out


def pct_change(curr: float, prev: float) -> Optional[float]:
    if prev is None or prev == 0:
        return None
    return round((curr - prev) / prev * 100, 1)


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    env_path = OUT / ".env.local"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())
    return env


def fetch_api(base: str, api_key: str) -> tuple[str, dict, list, list, list]:
    headers = {"Accept": "application/json", "X-API-Key": api_key}

    def get_json(path: str):
        req = Request(base.rstrip("/") + path, headers=headers)
        with urlopen(req, timeout=120) as res:
            return json.loads(res.read().decode("utf-8"))

    def get_text(path: str) -> str:
        req = Request(
            base.rstrip("/") + path,
            headers={**headers, "Accept": "text/csv,*/*"},
        )
        with urlopen(req, timeout=180) as res:
            return res.read().decode("utf-8")

    summary = get_json("/api/v1/summary?days=3650")
    monthly = get_json("/api/v1/monthly")
    routes = get_json("/api/v1/routes?days=3650&limit=25")
    top = get_json("/api/v1/passengers/top?days=3650&limit=50")
    csv_text = get_text("/api/v1/export/boardings.csv")
    return csv_text, summary, monthly, routes, top


# Longest available operational window (includes 2024 uploads)
BASE_START = date(2024, 1, 1)


def _mission_legs_from_rows(payload_rows: list[dict], base_start: date) -> list[MissionLeg]:
    """One MissionLeg per distinct flight_id in the filtered window."""
    by_fid: dict[str, dict] = {}
    for r in payload_rows:
        fd_raw = (r.get("flight_date") or "").strip()
        if not fd_raw:
            continue
        fd = date.fromisoformat(fd_raw[:10])
        if fd < base_start:
            continue
        fid = str(r.get("flight_id") or "").strip()
        if not fid:
            continue
        by_fid.setdefault(
            fid,
            {
                "id": int(fid) if fid.isdigit() else abs(hash(fid)) % (10**9),
                "flight_date": fd,
                "flight_time": r.get("flight_time"),
                "origin_code": r.get("origin_code"),
                "dest_code": r.get("dest_code"),
                "sheet_name": r.get("sheet_name"),
                "aircraft_reg": r.get("aircraft_reg"),
            },
        )
    return [
        MissionLeg(
            flight_id=v["id"],
            flight_date=v["flight_date"],
            flight_time=v.get("flight_time"),
            origin_code=v.get("origin_code"),
            dest_code=v.get("dest_code"),
            sheet_name=v.get("sheet_name"),
            aircraft_reg=v.get("aircraft_reg"),
        )
        for v in by_fid.values()
    ]


def compute(
    payload_rows: list[dict],
    summary: dict,
    monthly_api: list,
    routes: list,
    top: list,
    source: str,
    base_start: date = BASE_START,
) -> dict:
    boardings: list[tuple[int, date]] = []
    flight_leg_months: dict[str, set] = defaultdict(set)
    pax_dates: dict[int, list[date]] = defaultdict(list)

    for r in payload_rows:
        fd_raw = (r.get("flight_date") or "").strip()
        if not fd_raw or not r.get("passenger_id"):
            continue
        fd = date.fromisoformat(fd_raw[:10])
        if fd < base_start:
            continue
        pid = int(r["passenger_id"])
        boardings.append((pid, fd))
        pax_dates[pid].append(fd)
        fl = r.get("flight_id") or f"{fd}|{r.get('sheet_name')}|{r.get('flight_time')}"
        flight_leg_months[month_label(fd)].add(str(fl))

    # Sigtrip-style missions (connected same-day chains per aircraft)
    mission_month_counts = missions_by_month(
        _mission_legs_from_rows(payload_rows, base_start)
    )

    if not boardings:
        raise SystemExit("No boardings found")

    first_seen: dict[int, date] = {}
    for pid, fd in boardings:
        if pid not in first_seen or fd < first_seen[pid]:
            first_seen[pid] = fd

    data_start = min(fd for _, fd in boardings)
    # Anchor series at requested base month even if first boarding is later
    series_start = min(month_start(data_start), month_start(base_start))
    if series_start < month_start(base_start):
        series_start = month_start(base_start)
    data_end = max(fd for _, fd in boardings)
    months_all = iter_months(month_start(base_start), data_end)

    # Fill continuous month series (zeros for gaps)
    api_by = {
        r["month"]: r
        for r in monthly_api
        if r.get("month", "") >= month_label(base_start)
    }
    monthly: list[dict] = []
    cumulative = 0
    prev_row: Optional[dict] = None
    prev_active: Optional[dict] = None
    yoy_index: dict[str, dict] = {}

    for m0 in months_all:
        label = month_label(m0)
        m_end = month_end(m0)
        new_customers = sum(1 for fd in first_seen.values() if month_start(fd) == m0)
        cumulative += new_customers

        # Active unique in calendar month
        active = {
            pid
            for pid, dates in pax_dates.items()
            if any(month_start(d) == m0 for d in dates)
        }
        boardings_m = sum(1 for _, fd in boardings if month_start(fd) == m0)
        legs_m = len(flight_leg_months.get(label, set()))
        flights_m = mission_month_counts.get(label, 0)
        if label in api_by:
            boardings_m = api_by[label]["boardings"]
            # Prefer API missions when present; else local mission cut
            if api_by[label].get("missions") is not None:
                flights_m = api_by[label]["missions"]
            elif api_by[label].get("flight_count_unit") == "mission":
                flights_m = api_by[label]["flights"]
            else:
                flights_m = mission_month_counts.get(label, api_by[label]["flights"])
            legs_m = api_by[label].get("flight_legs", legs_m)
            unique_m = api_by[label]["unique_passengers"]
        else:
            unique_m = len(active)

        has_activity = boardings_m > 0
        # Known ingest gaps / thin months (re-check after each API refresh)
        KNOWN_GAPS = {
            "2026-06": "Mai-Jun_2026 incompleto na API (sem boardings em junho)",
        }
        data_gap = KNOWN_GAPS.get(label)
        if has_activity and boardings_m < 20:
            data_gap = (data_gap + " · " if data_gap else "") + f"mês com poucos boardings ({boardings_m})"

        # Rolling LTM ending this month (longest available window up to 12m)
        win_start = add_months(m0, -11)
        if win_start < month_start(data_start):
            win_start = month_start(data_start)
        ltm_counts: dict[int, int] = defaultdict(int)
        for pid, dates in pax_dates.items():
            n = sum(1 for d in dates if win_start <= d <= m_end)
            if n:
                ltm_counts[pid] = n
        ltm_unique = len(ltm_counts)
        ltm_repeat = sum(1 for n in ltm_counts.values() if n >= 2)
        repeat_pct = round(ltm_repeat / ltm_unique * 100, 1) if ltm_unique else 0.0

        row = {
            "month": label,
            "new_customers": new_customers,
            "cumulative_unique_customers": cumulative,
            "unique_passengers": unique_m,
            "boardings": boardings_m,
            "flights": flights_m,
            "missions": flights_m,
            "flight_legs": legs_m,
            "has_activity": has_activity,
            "data_gap": data_gap,
            "ltm_unique_customers": ltm_unique,
            "ltm_repeat_customers": ltm_repeat,
            "repeat_rate_pct": repeat_pct,
            "window_start": win_start.isoformat(),
            "window_end": m_end.isoformat(),
            "mom_new_pct": None,
            "mom_unique_pct": None,
            "mom_boardings_pct": None,
            "mom_cumulative_pct": None,
            "yoy_new_pct": None,
            "yoy_unique_pct": None,
            "yoy_boardings_pct": None,
            "yoy_cumulative_pct": None,
            "mom_vs_month": None,
            "yoy_vs_month": None,
        }

        # MoM / YoY on CUMULATIVE unique (primary growth view)
        if prev_row is not None:
            row["mom_cumulative_pct"] = pct_change(
                cumulative, prev_row["cumulative_unique_customers"]
            )
            row["mom_vs_month"] = prev_row["month"]
            # secondary: flow metrics vs previous active month
            if has_activity and prev_active is not None:
                row["mom_new_pct"] = pct_change(new_customers, prev_active["new_customers"])
                row["mom_unique_pct"] = pct_change(
                    unique_m, prev_active["unique_passengers"]
                )
                row["mom_boardings_pct"] = pct_change(
                    boardings_m, prev_active["boardings"]
                )

        prev_year = f"{m0.year - 1}-{m0.month:02d}"
        py = yoy_index.get(prev_year)
        # YoY cumulativo só com base comparável (evita % absurdos em meses quase vazios)
        if (
            py
            and cumulative > 0
            and py.get("cumulative_unique_customers", 0) >= 50
            and (py.get("boardings", 0) >= 50 or py.get("has_activity"))
        ):
            row["yoy_cumulative_pct"] = pct_change(
                cumulative, py["cumulative_unique_customers"]
            )
            row["yoy_vs_month"] = prev_year
            if has_activity and py.get("has_activity") and py.get("boardings", 0) >= 50:
                row["yoy_new_pct"] = pct_change(new_customers, py["new_customers"])
                row["yoy_unique_pct"] = pct_change(unique_m, py["unique_passengers"])
                row["yoy_boardings_pct"] = pct_change(boardings_m, py["boardings"])

        monthly.append(row)
        yoy_index[label] = row
        prev_row = row
        if has_activity:
            prev_active = row

    active_months = [r for r in monthly if r["has_activity"]]
    last = active_months[-1] if active_months else monthly[-1]
    yoy_points = [r for r in monthly if r["yoy_cumulative_pct"] is not None]

    # Recompute headline KPIs on the filtered window (not raw API all-time)
    uniques_window = len(first_seen)
    repeaters_window = sum(1 for dates in pax_dates.values() if len(dates) >= 2)
    legs_window = sum(len(v) for v in flight_leg_months.values())
    missions_window = sum(mission_month_counts.values())
    boardings_window = len(boardings)
    recurrence_window = (
        round(repeaters_window / uniques_window * 100, 1) if uniques_window else 0.0
    )

    # LTM missions (ending at latest active month) — by mission date
    ltm_mission_total = 0
    if last:
        ltm_start = date.fromisoformat(last["window_start"])
        ltm_end = date.fromisoformat(last["window_end"])
        ltm_mission_total = sum(
            1
            for m in assign_missions(_mission_legs_from_rows(payload_rows, base_start))
            if ltm_start <= m.flight_date <= ltm_end
        )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "base_start": base_start.isoformat(),
        "data_start": max(data_start, base_start).isoformat(),
        "data_end": data_end.isoformat(),
        "months_available": len(monthly),
        "summary": {
            "unique_customers_all_time": uniques_window,
            "total_boardings": boardings_window,
            "total_flights": missions_window,
            "total_missions": missions_window,
            "total_flight_legs": legs_window,
            "flight_count_unit": "mission",
            "ltm_missions": ltm_mission_total,
            "recurring_all_time": repeaters_window,
            "recurrence_rate_all_time": recurrence_window,
            "cumulative_unique_end": last["cumulative_unique_customers"],
            "latest_month": last["month"],
            "latest_mom_cumulative_pct": last["mom_cumulative_pct"],
            "latest_yoy_cumulative_pct": last["yoy_cumulative_pct"],
            "latest_mom_unique_pct": last["mom_unique_pct"],
            "latest_mom_boardings_pct": last["mom_boardings_pct"],
            "latest_yoy_unique_pct": last["yoy_unique_pct"],
            "latest_yoy_boardings_pct": last["yoy_boardings_pct"],
            "ltm_unique_customers": last["ltm_unique_customers"],
            "ltm_repeat_rate_pct": last["repeat_rate_pct"],
            "yoy_months_available": len(yoy_points),
            "api_unique_unfiltered": summary.get("unique_passengers"),
        },
        "monthly": monthly,
        "top_routes": routes,
        "top_passengers": [
            {
                "name": r.get("name"),
                "identity_key": r.get("identity_key", ""),
                "boardings": r.get("boardings", 0),
                "distinct_dates": r.get("distinct_dates", 0),
                "first_in_window": r.get("first_in_window") or "",
                "last_in_window": r.get("last_in_window") or "",
            }
            for r in top
        ],
    }


HTML = r'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>REVO · Customer growth (MoM / YoY)</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <script src="./data.js"></script>
  <style>
    :root {
      --bg: #eef2ec; --ink: #142018; --muted: #5a675c; --panel: #fffef9;
      --line: #d5ddd6; --a: #0b6b52; --b: #c45c26; --c: #2f5d9f;
      --font: "Iowan Old Style", Palatino, Georgia, serif;
      --sans: "Avenir Next", "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; color: var(--ink); font-family: var(--sans);
      background:
        radial-gradient(ellipse 60% 40% at 100% 0%, #dceadf, transparent 55%),
        radial-gradient(ellipse 50% 35% at 0% 100%, #efe4d6, transparent 50%),
        var(--bg);
    }
    main { max-width: 1180px; margin: 0 auto; padding: 36px 20px 80px; }
    h1 { font-family: var(--font); font-size: clamp(2.2rem, 5vw, 3.3rem); margin: 0; letter-spacing: -0.02em; }
    .lede { color: var(--muted); max-width: 46em; line-height: 1.5; margin: 10px 0 0; }
    .meta { margin-top: 12px; color: var(--muted); font-size: 0.9rem; }
    .meta a { color: var(--a); }
    .kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 26px 0; }
    .kpis article { background: var(--panel); border: 1px solid var(--line); padding: 14px 16px; }
    .kpis .label { display: block; font-size: 0.7rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
    .kpis strong { font-family: var(--font); font-size: 1.7rem; }
    .kpis .sub { display: block; margin-top: 4px; color: var(--muted); font-size: 0.85rem; }
    section { margin-top: 28px; }
    h2 { font-family: var(--font); font-size: 1.35rem; margin: 0 0 6px; }
    .help { color: var(--muted); margin: 0 0 14px; font-size: 0.95rem; }
    .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .charts.full { grid-template-columns: 1fr; }
    .box { background: var(--panel); border: 1px solid var(--line); padding: 14px; }
    .box h3 { margin: 0 0 10px; color: var(--muted); font-size: 0.95rem; }
    .table-wrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); }
    table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
    th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
    th { color: var(--muted); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; }
    td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
    .pos { color: #0b6b52; } .neg { color: #b33b2b; } .na { color: #9aa39b; }
    @media (max-width: 900px) { .kpis, .charts { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
<main>
  <header>
    <h1>REVO</h1>
    <p class="lede">Crescimento de clientes a partir de janeiro/2024 (API Manifests) — cumulativo, MoM e YoY. SIAV→SIAV excluídos. Meses incompletos aparecem com alerta.</p>
    <p class="meta" id="meta"></p>
    <p class="meta"><a href="./revo-customer-kpis.xlsx">Baixar Excel com gráficos</a></p>
  </header>

  <div class="kpis" id="kpis"></div>

  <section>
    <h2>Cumulativo unique customers</h2>
    <p class="help">Base acumulada de clientes únicos desde janeiro/2024 (sem SIAV→SIAV).</p>
    <div class="charts full"><div class="box"><canvas id="chartCum" height="260"></canvas></div></div>
  </section>

  <section>
    <h2>Variação do cumulativo · MoM</h2>
    <p class="help">Crescimento percentual do cumulativo de unique vs o mês anterior.</p>
    <div class="charts full">
      <div class="box"><h3>MoM % · cumulativo unique</h3><canvas id="chartMomCum" height="260"></canvas></div>
    </div>
  </section>

  <section>
    <h2>Variação do cumulativo · YoY</h2>
    <p class="help">Crescimento percentual do cumulativo vs o mesmo mês do ano anterior (quando existir).</p>
    <div class="charts full">
      <div class="box"><h3>YoY % · cumulativo unique</h3><canvas id="chartYoyCum" height="260"></canvas></div>
    </div>
  </section>

  <section>
    <h2>Novos clientes e repeat rate (rolling)</h2>
    <div class="charts">
      <div class="box"><h3>Novos clientes / mês</h3><canvas id="chartNew" height="240"></canvas></div>
      <div class="box"><h3>Repeat rate rolling (até 12m)</h3><canvas id="chartRepeat" height="240"></canvas></div>
    </div>
  </section>

  <section>
    <h2>Qualidade da base</h2>
    <p class="help" id="quality"></p>
  </section>

  <section>
    <h2>Série mensal completa</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Mês</th>
            <th class="num">Novos</th>
            <th class="num">Cumulativo</th>
            <th class="num">Unique</th>
            <th class="num">Boardings</th>
            <th class="num">MoM cumul.%</th>
            <th class="num">YoY cumul.%</th>
            <th class="num">Repeat %</th>
          </tr>
        </thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </section>
</main>
<script>
const D = window.KPI_DATA || {};
const s = D.summary || {};
const monthly = D.monthly || [];
const fmt = (v, suffix='%') => v == null ? '—' : `${v > 0 ? '+' : ''}${v}${suffix}`;
const cls = (v) => v == null ? 'na' : (v >= 0 ? 'pos' : 'neg');

const gaps = monthly.filter(r => r.data_gap);
document.getElementById('quality').innerHTML = gaps.length
  ? ('Lacunas de ingestão: ' + gaps.map(r => `<strong>${r.month}</strong> — ${r.data_gap}`).join(' · ') + '. Suba os Excel faltantes na API de Manifests para preencher.')
  : 'Nenhuma lacuna conhecida no período.';

document.getElementById('meta').textContent =
  `Fonte: ${D.source || '—'} · base ${D.base_start || D.data_start || '—'} → ${D.data_end || '—'} · ${D.months_available || 0} meses · gerado ${D.generated_at || '—'}`;

document.getElementById('kpis').innerHTML = [
  ['Unique (base jan/24)', s.unique_customers_all_time, `${s.total_boardings || 0} boardings`],
  ['Missões (corte Sigtrip)', s.total_missions ?? s.total_flights, `${s.total_flight_legs || '—'} pernas · LTM ${s.ltm_missions ?? '—'}`],
  ['Cumulativo final', s.cumulative_unique_end, `até ${s.latest_month || '—'}`],
  ['MoM cumulativo', fmt(s.latest_mom_cumulative_pct), `último mês ${s.latest_month || '—'}`],
  ['YoY cumulativo', fmt(s.latest_yoy_cumulative_pct), s.yoy_months_available ? `${s.yoy_months_available} meses com YoY` : 'sem par YoY ainda'],
  ['LTM unique / repeat', s.ltm_unique_customers, `repeat ${s.ltm_repeat_rate_pct ?? '—'}%`],
].map(([l,v,sub]) => `<article><span class="label">${l}</span><strong>${v ?? '—'}</strong><span class="sub">${sub || ''}</span></article>`).join('');

document.getElementById('tbody').innerHTML = monthly.map(r => `
  <tr>
    <td>${r.month}${r.data_gap ? ` <span class="na">⚠ ${r.data_gap}</span>` : (r.has_activity ? '' : ' <span class="na">(sem voos)</span>')}</td>
    <td class="num">${r.new_customers}</td>
    <td class="num">${r.cumulative_unique_customers}</td>
    <td class="num">${r.unique_passengers}</td>
    <td class="num">${r.boardings}</td>
    <td class="num ${cls(r.mom_cumulative_pct)}">${fmt(r.mom_cumulative_pct)}</td>
    <td class="num ${cls(r.yoy_cumulative_pct)}">${fmt(r.yoy_cumulative_pct)}</td>
    <td class="num">${r.repeat_rate_pct}%</td>
  </tr>`).join('');

const labels = monthly.map(r => r.month);
const active = monthly.filter(r => r.has_activity);
const momCum = monthly.filter(r => r.mom_cumulative_pct != null);
const yoyCum = monthly.filter(r => r.yoy_cumulative_pct != null);
const grid = { color: 'rgba(20,32,24,0.08)' };
const line = (id, datasets, opts={}) => new Chart(document.getElementById(id), {
  type: 'line',
  data: { labels: opts.labels || labels, datasets },
  options: {
    plugins: { legend: { display: !!opts.legend, position: 'bottom' } },
    scales: {
      x: { grid },
      y: {
        grid,
        beginAtZero: !!opts.beginAtZero,
        ticks: opts.pct ? { callback: v => v + '%' } : undefined,
      },
    },
  },
});

line('chartCum', [{
  label: 'Cumulativo unique',
  data: monthly.map(r => r.cumulative_unique_customers),
  borderColor: '#0b6b52', backgroundColor: 'rgba(11,107,82,0.12)', fill: true, tension: 0.25, pointRadius: 3,
}], { beginAtZero: true });

line('chartMomCum', [{
  label: 'MoM cumulativo %',
  data: momCum.map(r => r.mom_cumulative_pct),
  borderColor: '#c45c26', backgroundColor: 'rgba(196,92,38,0.12)', fill: true, tension: 0.2, pointRadius: 3, spanGaps: true,
}], { labels: momCum.map(r => r.month), pct: true });

line('chartYoyCum', [{
  label: 'YoY cumulativo %',
  data: yoyCum.map(r => r.yoy_cumulative_pct),
  borderColor: '#2f5d9f', backgroundColor: 'rgba(47,93,159,0.12)', fill: true, tension: 0.2, pointRadius: 3, spanGaps: true,
}], { labels: yoyCum.map(r => r.month), pct: true });

line('chartNew', [{
  label: 'Novos',
  data: active.map(r => r.new_customers),
  borderColor: '#0b6b52', tension: 0.25, pointRadius: 3,
}], { labels: active.map(r => r.month), beginAtZero: true });

line('chartRepeat', [{
  label: 'Repeat %',
  data: active.map(r => r.repeat_rate_pct),
  borderColor: '#c45c26', backgroundColor: 'rgba(196,92,38,0.10)', fill: true, tension: 0.25, pointRadius: 3,
}], { labels: active.map(r => r.month), pct: true, beginAtZero: true });
</script>
</body>
</html>
'''


def write_excel(data: dict, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.chart import LineChart, Reference
    from openpyxl.chart.series import SeriesLabel
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo"
    ws["A1"] = "REVO · Customer growth MoM / YoY"
    ws["A1"].font = Font(size=16, bold=True)
    ws["A2"] = (
        f"Fonte {data['source']} · base {data.get('base_start') or data['data_start']} "
        f"→ {data['data_end']} · {data['months_available']} meses"
    )
    s = data["summary"]
    cards = [
        ("Unique (base jan/24)", s.get("unique_customers_all_time")),
        ("Cumulativo final", s.get("cumulative_unique_end")),
        ("MoM cumulativo %", s.get("latest_mom_cumulative_pct")),
        ("YoY cumulativo %", s.get("latest_yoy_cumulative_pct")),
        ("Boardings", s.get("total_boardings")),
        ("Missões (Sigtrip)", s.get("total_missions") or s.get("total_flights")),
        ("Pernas (legs)", s.get("total_flight_legs")),
        ("Missões LTM", s.get("ltm_missions")),
    ]
    for i, (lab, val) in enumerate(cards):
        ws.cell(4, 1 + i, lab)
        ws.cell(5, 1 + i, val if val is not None else "—")

    ws_m = wb.create_sheet("Mensal")
    headers = [
        "Mês", "Novos", "Cumulativo", "Unique", "Boardings", "Missões",
        "MoM cumulativo %", "YoY cumulativo %",
        "LTM unique", "Repeat %",
    ]
    for i, h in enumerate(headers, 1):
        ws_m.cell(1, i, h)
    for r_i, r in enumerate(data["monthly"], 2):
        vals = [
            r["month"], r["new_customers"], r["cumulative_unique_customers"],
            r["unique_passengers"], r["boardings"], r["flights"],
            r["mom_cumulative_pct"], r["yoy_cumulative_pct"],
            r["ltm_unique_customers"], r["repeat_rate_pct"],
        ]
        for c, v in enumerate(vals, 1):
            ws_m.cell(r_i, c, v if v is not None else None)
    last = 1 + len(data["monthly"])

    def add_line(title, min_col, anchor, y_pct=False):
        ch = LineChart()
        ch.title = title
        ch.height = 10
        ch.width = 16
        ch.add_data(Reference(ws_m, min_col=min_col, min_row=1, max_row=last), titles_from_data=True)
        ch.set_categories(Reference(ws_m, min_col=1, min_row=2, max_row=last))
        ws_m.add_chart(ch, anchor)

    add_line("Cumulativo unique", 3, "L2")
    add_line("MoM cumulativo %", 7, "L20", y_pct=True)
    add_line("YoY cumulativo %", 8, "L38", y_pct=True)

    ws_r = wb.create_sheet("Top Rotas")
    for i, h in enumerate(["Rota", "Boardings", "Flights"], 1):
        ws_r.cell(1, i, h)
    for r_i, r in enumerate(data.get("top_routes") or [], 2):
        ws_r.cell(r_i, 1, r.get("route"))
        ws_r.cell(r_i, 2, r.get("boardings"))
        ws_r.cell(r_i, 3, r.get("flights"))

    ws_p = wb.create_sheet("Top Passageiros")
    for i, h in enumerate(["Nome", "Identity", "Boardings", "Datas", "Primeiro", "Último"], 1):
        ws_p.cell(1, i, h)
    for r_i, r in enumerate(data.get("top_passengers") or [], 2):
        ws_p.cell(r_i, 1, r.get("name"))
        ws_p.cell(r_i, 2, r.get("identity_key"))
        ws_p.cell(r_i, 3, r.get("boardings"))
        ws_p.cell(r_i, 4, r.get("distinct_dates"))
        ws_p.cell(r_i, 5, r.get("first_in_window"))
        ws_p.cell(r_i, 6, r.get("last_in_window"))

    wb.save(path)


def main() -> None:
    env = load_env()
    base = env.get("MANIFESTS_API_BASE") or "https://web-production-9b4c2.up.railway.app"
    api_key = env.get("API_KEY") or ""

    try:
        csv_text, summary, monthly_api, routes, top = fetch_api(base, api_key)
        (OUT / "boardings_api.csv").write_text(csv_text, encoding="utf-8")
        source = f"api:{base}"
        print(f"Fetched API {base}")
    except Exception as exc:
        print(f"API fetch failed ({exc}); using local boardings_api.csv")
        csv_text = (OUT / "boardings_api.csv").read_text(encoding="utf-8")
        summary = json.loads((OUT / "summary.json").read_text())
        monthly_api = json.loads((OUT / "monthly.json").read_text())
        routes = json.loads((OUT / "routes.json").read_text()) if (OUT / "routes.json").exists() else []
        top = json.loads((OUT / "top_passengers.json").read_text()) if (OUT / "top_passengers.json").exists() else []
        source = f"local-csv+cache ({base})"

    rows = drop_cancelled_rows(list(csv.DictReader(StringIO(csv_text))))
    # Persist a clean local CSV (no cancelled tabs)
    if rows:
        buf = StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        (OUT / "boardings_api.csv").write_text(buf.getvalue(), encoding="utf-8")

    data = compute(rows, summary, monthly_api, routes, top, source)

    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (OUT / "monthly.json").write_text(json.dumps(monthly_api, indent=2), encoding="utf-8")
    (OUT / "data.js").write_text(
        "window.KPI_DATA = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    (OUT / "index.html").write_text(HTML, encoding="utf-8")
    write_excel(data, OUT / "revo-customer-kpis.xlsx")

    print("Dashboard:", OUT / "index.html")
    print("Excel:", OUT / "revo-customer-kpis.xlsx")
    print(json.dumps(data["summary"], indent=2))
    print("months", data["months_available"], data["data_start"], "→", data["data_end"])
    print(
        "MoM cumul",
        [(r["month"], r["mom_cumulative_pct"]) for r in data["monthly"] if r["mom_cumulative_pct"] is not None][-6:],
    )
    print(
        "YoY cumul",
        [(r["month"], r["yoy_cumulative_pct"]) for r in data["monthly"] if r["yoy_cumulative_pct"] is not None],
    )


if __name__ == "__main__":
    main()
