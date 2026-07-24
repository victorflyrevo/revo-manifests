# Customer KPI dashboard (local)

Builds `index.html` + `revo-customer-kpis.xlsx` from the Manifests API.

**Hero metric: recorrência LTM** (últimos 12 meses) — distribuição 1× … 20× e >20, cortes ≥2 e ≥4, delta vs o LTM de 12 meses atrás, e snapshots Jun/2026 · Dez/2025 · Dez/2024.

Flight counts use the **Sigtrip mission cut** (`app.missions`): connected same-day legs on the same aircraft. Recurrence counts **boardings per passenger** inside the LTM window (SIAV→SIAV and cancelled sheets excluded).

## Setup

```bash
# tools/kpi-dashboard/.env.local
MANIFESTS_API_BASE=https://web-production-9b4c2.up.railway.app
API_KEY=…

# Optional — Salesforce hours + recognized revenue
SF_DOMAIN=login
SF_USERNAME=…
SF_PASSWORD=…
SF_TOKEN=…
```

## Build

From repo root (or this folder):

```bash
# optional Salesforce pull (needs simple-salesforce + SF_* env)
python3 tools/kpi-dashboard/pull_salesforce_kpis.py

python3 tools/kpi-dashboard/build_full_dashboard.py
```

Outputs are written next to the script (gitignored): `index.html`, `data.js`, `revo-customer-kpis.xlsx`, `salesforce_kpis.json`.

## Recorrência LTM

| Campo | Significado |
|---|---|
| Unique customers | Passageiros distintos na base (jan/2024 → hoje) |
| Customers | Boardings (cada embarque conta) |
| Unique LTM | Passageiros com ≥1 boarding na janela de até 12 meses |
| ≥2 / ≥4 | Passageiros com 2+ / 4+ boardings na mesma janela |
| Δ vs −12m | Diferença absoluta vs o LTM que terminava no mesmo mês do ano anterior |
| Freq LTM mensal | Distribuição 1×…20× / >20 para cada mês-fim de janela |
| Freq todo o período | Mesma distribuição em toda a base (jan/2024 → último dado), sem corte LTM |
| Snapshots | Cortes fixos: `2026-06`, `2025-12`, `2024-12` |

| Horas SBGR / Resto / Shuttle | Salesforce `TempoMissao__c` (min÷60), voos Executado |
| Valor pago (voo) | `Servico.ValorPago__c` via Empenho, no mês do voo (relatório Financeiro) |
| Receita reconhecida | `Empenho.ValorReconhecimentoReceita__c` (desde ~mar/2026) |
| Corporate mobility % | Conta Faturamento PJ / reconhecido |
| Subscription | Pagamento Record Type = Voucher RevoSeats |

Excel: abas **Glossário**, **Horas SBGR**, **Faturamento SF**, **Recorrência LTM**, **Freq LTM Mensal**, **Mensal**.
