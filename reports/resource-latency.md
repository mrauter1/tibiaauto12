# Original screenshot resource latency

Overall: **PASS**

Offline algorithm timing on preserved original full screenshots. Repeated reads of the same image are not a live session and do not measure capture or input latency.

| Source | Role | Discovery | Warm reads | Warm p50/p95/max | Exact + row correspondence | Budget | Result |
|---|---|---:|---:|---:|:---:|:---:|:---:|
| `user-paulistinha-20261013` | `positive_acceptance` | 6270.26 ms | 50 | 4.65 ms/5.27 ms/5.60 ms | yes | not declared | PASS |
| `tibiadecorations-2024-full-window` | `negative_safety_control` | 9784.26 ms | 0 | —/—/— | — | not declared | PASS |
| `dfg-retro-pvp-rp535` | `positive_acceptance` | 6394.84 ms | 50 | 7.82 ms/10.43 ms/12.70 ms | yes | not declared | PASS |
| `dfg-ustebra-ek177` | `positive_acceptance` | 5531.37 ms | 50 | 7.24 ms/10.50 ms/13.15 ms | yes | not declared | PASS |
| `dfg-yonabra-ek195` | `positive_acceptance` | 2595.22 ms | 50 | 25.80 ms/30.70 ms/35.39 ms | yes | not declared | PASS |
| `dfg-gentebra-rp193-PPNB` | `positive_acceptance` | 2602.50 ms | 50 | 8.09 ms/9.05 ms/11.65 ms | yes | not declared | PASS |
| `dfg-lutabra-ed312-IBIV` | `positive_acceptance` | 2596.22 ms | 50 | 7.68 ms/8.82 ms/9.91 ms | yes | not declared | PASS |
| `dfg-loyalty-ek277-OL5J` | `ambiguous_safety_control` | 5539.53 ms | 0 | —/—/— | — | not declared | PASS |
| `dfg-calmera-ek246-D9BJ` | `positive_acceptance` | 2347.69 ms | 50 | 12.28 ms/15.26 ms/15.75 ms | yes | not declared | PASS |

Aggregate cached reads: **350** samples; p50/p95/max **7.99/26.17/35.39 ms**.
