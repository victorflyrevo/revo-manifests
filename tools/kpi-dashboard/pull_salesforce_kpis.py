#!/usr/bin/env python3
"""Pull shuttle hours (SBGR vs rest) and sales-base KPIs from Salesforce.

Reads SF_* from tools/kpi-dashboard/.env.local (or process env) and writes
salesforce_kpis.json next to this script for the HTML/Excel dashboard builder.

Revenue matches Financeiro reports (Validação Weekly KPI):
  Empenho__c → Servico__c → Voo__c by flight date
  - faturamento (valor_pago): unique Servico.ValorPago__c
  - corporate %: Conta Faturamento PJ share of empenho/valor pago
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parent


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    env_path = OUT / ".env.local"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def involves_sbgr(trecho: dict) -> bool:
    parts = [
        trecho.get("CodigoOrigem__c"),
        trecho.get("CodigoDestino__c"),
        trecho.get("OrigemTexto__c"),
        trecho.get("DestinoTexto__c"),
    ]
    blob = " ".join(str(p or "").upper() for p in parts)
    return "SBGR" in blob or "GUARULHOS" in blob


def query_all(sf, soql: str) -> list[dict]:
    rows: list[dict] = []
    result = sf.query(soql)
    rows.extend(result["records"])
    while not result["done"]:
        result = sf.query_more(result["nextRecordsUrl"], True)
        rows.extend(result["records"])
    return rows


def ltm_bucket(monthly: dict, end: str, months: int = 12) -> dict:
    y, m = map(int, end.split("-"))
    keys = []
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    valor_pago = corp = 0.0
    n = 0
    for k in keys:
        b = monthly.get(k)
        if not b:
            continue
        valor_pago += b["valor_pago"]
        corp += b["corporate_pj"]
        n += b["n"]
    return {
        "end": end,
        "valor_pago": round(valor_pago, 2),
        "n": n,
        "corporate_pj": round(corp, 2),
        "corporate_pj_pct": round(corp / valor_pago * 100, 2) if valor_pago else 0.0,
    }


def account_rt_name(servico: dict) -> str:
    fat = ((servico.get("ContaFaturamento__r") or {}).get("RecordType") or {}).get(
        "Name"
    ) or ""
    if fat:
        return fat
    return (
        ((servico.get("ContaComprador__r") or {}).get("RecordType") or {}).get("Name")
        or ""
    )


def main() -> None:
    from simple_salesforce import Salesforce

    env = load_env()
    username = env.get("SF_USERNAME") or ""
    password = env.get("SF_PASSWORD") or ""
    token = env.get("SF_TOKEN") or ""
    domain = env.get("SF_DOMAIN") or "login"
    if not (username and password and token):
        raise SystemExit(
            "Missing SF_USERNAME / SF_PASSWORD / SF_TOKEN in .env.local or environment"
        )

    sf = Salesforce(
        username=username,
        password=password,
        security_token=token,
        domain=domain,
    )
    print("Connected:", sf.sf_instance)

    # --- Hours ---
    trechos = query_all(
        sf,
        """
        SELECT Voo__c, CodigoOrigem__c, CodigoDestino__c, OrigemTexto__c, DestinoTexto__c,
               Voo__r.DiaVoo__c, Voo__r.Tipo__c, Voo__r.Status__c, Voo__r.TempoMissao__c
        FROM Trecho__c
        WHERE Voo__r.Status__c = 'Executado' AND Voo__r.DiaVoo__c = LAST_N_DAYS:800
        """,
    )

    voo: dict[str, dict] = {}
    for r in trechos:
        vid = r.get("Voo__c")
        if not vid:
            continue
        if vid not in voo:
            vr = r.get("Voo__r") or {}
            voo[vid] = {
                "day": vr.get("DiaVoo__c"),
                "tipo": vr.get("Tipo__c") or "",
                "minutes": float(vr.get("TempoMissao__c") or 0),
                "sbgr": False,
            }
        if involves_sbgr(r):
            voo[vid]["sbgr"] = True

    monthly_h: dict[str, dict] = defaultdict(
        lambda: {
            "hours_all": 0.0,
            "hours_sbgr": 0.0,
            "hours_resto": 0.0,
            "hours_shuttle": 0.0,
            "hours_shuttle_sbgr": 0.0,
            "flights": 0,
            "flights_sbgr": 0,
            "flights_shuttle": 0,
        }
    )
    for v in voo.values():
        if not v["day"] or v["minutes"] <= 0:
            continue
        m = v["day"][:7]
        h = v["minutes"] / 60.0
        b = monthly_h[m]
        b["hours_all"] += h
        b["flights"] += 1
        if v["sbgr"]:
            b["hours_sbgr"] += h
            b["flights_sbgr"] += 1
        else:
            b["hours_resto"] += h
        if "Shuttle" in v["tipo"]:
            b["hours_shuttle"] += h
            b["flights_shuttle"] += 1
            if v["sbgr"]:
                b["hours_shuttle_sbgr"] += h

    months_h = sorted(monthly_h)
    max_all = max(months_h, key=lambda m: monthly_h[m]["hours_all"])
    max_sbgr = max(months_h, key=lambda m: monthly_h[m]["hours_sbgr"])
    max_shuttle = max(months_h, key=lambda m: monthly_h[m]["hours_shuttle"])

    # --- Sales base (Empenho / Servico / Voo) ---
    empenhos = query_all(
        sf,
        """
        SELECT Servico__c, ValorEmpenho__c,
               Servico__r.ValorPago__c,
               Servico__r.Voo__r.DiaVoo__c, Servico__r.Voo__r.DataHoraVoo__c,
               Servico__r.ContaFaturamento__r.RecordType.Name,
               Servico__r.ContaComprador__r.RecordType.Name
        FROM Empenho__c
        WHERE Servico__r.Voo__r.DataHoraVoo__c = LAST_N_DAYS:800
           OR Servico__r.Voo__r.DiaVoo__c = LAST_N_DAYS:800
        """,
    )

    monthly_r: dict[str, dict] = defaultdict(
        lambda: {
            "valor_pago_by_servico": {},
            "n": 0,
            "corporate_pj": 0.0,
        }
    )

    for r in empenhos:
        serv = r.get("Servico__r") or {}
        vr = serv.get("Voo__r") or {}
        day = vr.get("DiaVoo__c") or (vr.get("DataHoraVoo__c") or "")[:10]
        if not day:
            continue
        m = day[:7]
        b = monthly_r[m]
        emp = float(r.get("ValorEmpenho__c") or 0)
        pago = float(serv.get("ValorPago__c") or 0)
        sid = r.get("Servico__c")
        if sid:
            b["valor_pago_by_servico"][sid] = pago
        b["n"] += 1
        if "Jurídica" in account_rt_name(serv):
            b["corporate_pj"] += emp

    monthly_out: dict[str, dict] = {}
    for m, b in monthly_r.items():
        valor_pago = sum(b["valor_pago_by_servico"].values())
        corp = b["corporate_pj"]
        monthly_out[m] = {
            "valor_pago": round(valor_pago, 2),
            "n": b["n"],
            "n_servicos": len(b["valor_pago_by_servico"]),
            "corporate_pj": round(corp, 2),
            "corporate_pj_pct": round(corp / valor_pago * 100, 2) if valor_pago else 0.0,
        }

    payload = {
        "generated_from": f"Salesforce API {sf.sf_instance}",
        "hours": {
            "unit": "hours",
            "method": (
                "Voo__c.TempoMissao__c (minutes)/60 for Status='Executado'; "
                "SBGR if any Trecho origin/dest involves SBGR/Guarulhos; "
                "Shuttle = Tipo contains Shuttle; full mission hours attributed "
                "to SBGR when the flight touches SBGR"
            ),
            "monthly": {
                m: {
                    k: (round(v, 3) if isinstance(v, float) else v)
                    for k, v in monthly_h[m].items()
                }
                for m in months_h
            },
            "historic_max": {
                "all": {
                    "month": max_all,
                    "hours": round(monthly_h[max_all]["hours_all"], 3),
                },
                "sbgr": {
                    "month": max_sbgr,
                    "hours": round(monthly_h[max_sbgr]["hours_sbgr"], 3),
                },
                "shuttle": {
                    "month": max_shuttle,
                    "hours": round(monthly_h[max_shuttle]["hours_shuttle"], 3),
                },
            },
            "latest_complete_month": "2026-06",
            "latest": {
                k: (round(v, 3) if isinstance(v, float) else v)
                for k, v in monthly_h.get("2026-06", {}).items()
            },
        },
        "revenue": {
            "definition": {
                "source": (
                    "Empenho__c + Servico__c + Voo__c "
                    "(relatório Financeiro · Validação Weekly KPI)"
                ),
                "valor_pago": (
                    "Soma de Servico.ValorPago__c distintos por voo "
                    "(equivale a Empenho.ValorEmpenho__c); data = DiaVoo/DataHoraVoo"
                ),
                "corporate_mobility": (
                    "Conta Faturamento (fallback Comprador) Record Type "
                    "Cliente - Pessoa Jurídica / faturamento"
                ),
            },
            "monthly": monthly_out,
            "snapshots": {
                "ltm_2026_06": ltm_bucket(monthly_out, "2026-06"),
                "ltm_2025_12": ltm_bucket(monthly_out, "2025-12"),
                "ltm_2024_12": ltm_bucket(monthly_out, "2024-12"),
            },
        },
    }

    out_path = OUT / "salesforce_kpis.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Wrote", out_path)
    print("Hours max all", payload["hours"]["historic_max"]["all"])
    print("Jun/2026 hours", payload["hours"]["latest"])
    print("May/2026 revenue", monthly_out.get("2026-05"))
    print("LTM Jun/2026 revenue", payload["revenue"]["snapshots"]["ltm_2026_06"])


if __name__ == "__main__":
    main()
