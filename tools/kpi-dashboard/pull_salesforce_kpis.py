#!/usr/bin/env python3
"""Pull shuttle hours (SBGR vs rest) and recognized revenue KPIs from Salesforce.

Reads SF_* from tools/kpi-dashboard/.env.local (or process env) and writes
salesforce_kpis.json next to this script for the HTML/Excel dashboard builder.
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


def ltm_revenue(monthly: dict, end: str, months: int = 12) -> dict:
    y, m = map(int, end.split("-"))
    keys = []
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    tot = corp = sub = 0.0
    n = 0
    by_type: dict[str, float] = defaultdict(float)
    for k in keys:
        b = monthly.get(k)
        if not b:
            continue
        tot += b["recognized"]
        corp += b["corporate_pj"]
        sub += b["subscription_revo_seats"]
        n += b["n"]
        for t, v in b["by_type"].items():
            by_type[t] += v
    return {
        "end": end,
        "recognized": round(tot, 2),
        "n": n,
        "corporate_pj": round(corp, 2),
        "corporate_pj_pct": round(corp / tot * 100, 2) if tot else 0.0,
        "subscription_revo_seats": round(sub, 2),
        "subscription_pct": round(sub / tot * 100, 2) if tot else 0.0,
        "by_type": {
            k: round(v, 2) for k, v in sorted(by_type.items(), key=lambda x: -x[1])
        },
    }


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

    rt_map = {
        rt["recordTypeId"]: rt["name"]
        for rt in sf.Opportunity.describe()["recordTypeInfos"]
    }
    acct_rt = {
        rt["recordTypeId"]: rt["name"]
        for rt in sf.Account.describe()["recordTypeInfos"]
    }
    revo_seats_rt = {k for k, v in rt_map.items() if v == "Revo Seats"}

    # --- Hours ---
    trechos: list[dict] = []
    result = sf.query(
        """
        SELECT Voo__c, CodigoOrigem__c, CodigoDestino__c, OrigemTexto__c, DestinoTexto__c,
               Voo__r.DiaVoo__c, Voo__r.Tipo__c, Voo__r.Status__c, Voo__r.TempoMissao__c
        FROM Trecho__c
        WHERE Voo__r.Status__c = 'Executado' AND Voo__r.DiaVoo__c = LAST_N_DAYS:800
        """
    )
    trechos.extend(result["records"])
    while not result["done"]:
        result = sf.query_more(result["nextRecordsUrl"], True)
        trechos.extend(result["records"])

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

    # --- Revenue ---
    opps: list[dict] = []
    result = sf.query(
        """
        SELECT CloseDate, Amount, Type, StageName, RecordTypeId,
               Account.RecordTypeId, TotalRevoSeats__c
        FROM Opportunity
        WHERE StageName = 'Pago' AND CloseDate = LAST_N_DAYS:900 AND Amount != null
        """
    )
    opps.extend(result["records"])
    while not result["done"]:
        result = sf.query_more(result["nextRecordsUrl"], True)
        opps.extend(result["records"])

    monthly_r: dict[str, dict] = defaultdict(
        lambda: {
            "recognized": 0.0,
            "n": 0,
            "corporate_pj": 0.0,
            "consumer_pf": 0.0,
            "other_acct": 0.0,
            "subscription_revo_seats": 0.0,
            "subscription_n": 0,
            "by_type": defaultdict(float),
        }
    )
    for r in opps:
        m = (r.get("CloseDate") or "")[:7]
        if not m:
            continue
        amt = float(r.get("Amount") or 0)
        b = monthly_r[m]
        b["recognized"] += amt
        b["n"] += 1
        b["by_type"][r.get("Type") or "Unknown"] += amt
        if r.get("RecordTypeId") in revo_seats_rt:
            b["subscription_revo_seats"] += amt
            b["subscription_n"] += 1
        art = acct_rt.get((r.get("Account") or {}).get("RecordTypeId"), "")
        if "Jurídica" in art:
            b["corporate_pj"] += amt
        elif "Física" in art or "pessoal" in art.lower():
            b["consumer_pf"] += amt
        else:
            b["other_acct"] += amt

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
                "recognized": "Opportunity.StageName = 'Pago' (CloseDate)",
                "corporate_mobility": (
                    "Account.RecordType = Cliente - Pessoa Jurídica"
                ),
                "subscription": "Opportunity.RecordType = Revo Seats",
            },
            "monthly": {
                m: {
                    "recognized": round(b["recognized"], 2),
                    "n": b["n"],
                    "corporate_pj": round(b["corporate_pj"], 2),
                    "corporate_pj_pct": round(
                        b["corporate_pj"] / b["recognized"] * 100, 2
                    )
                    if b["recognized"]
                    else 0,
                    "consumer_pf": round(b["consumer_pf"], 2),
                    "subscription_revo_seats": round(b["subscription_revo_seats"], 2),
                    "subscription_pct": round(
                        b["subscription_revo_seats"] / b["recognized"] * 100, 2
                    )
                    if b["recognized"]
                    else 0,
                    "subscription_n": b["subscription_n"],
                    "by_type": {k: round(v, 2) for k, v in b["by_type"].items()},
                }
                for m, b in monthly_r.items()
            },
            "snapshots": {
                "ltm_2026_06": ltm_revenue(monthly_r, "2026-06"),
                "ltm_2025_12": ltm_revenue(monthly_r, "2025-12"),
                "ltm_2024_12": ltm_revenue(monthly_r, "2024-12"),
            },
        },
    }

    out_path = OUT / "salesforce_kpis.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Wrote", out_path)
    print("Hours max all", payload["hours"]["historic_max"]["all"])
    print("Jun/2026 hours", payload["hours"]["latest"])
    print("LTM Jun/2026 revenue", payload["revenue"]["snapshots"]["ltm_2026_06"])


if __name__ == "__main__":
    main()
