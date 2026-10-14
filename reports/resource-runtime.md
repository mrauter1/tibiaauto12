# Historical artificial-relocation runtime diagnostic

This archived run generated moved-panel images. It is **not current acceptance
evidence** and does not establish native rendering or live recovery. Two sources
then described as full screenshots are now correctly classified as source crops.
Use [current originals latency](resource-latency.md) and the
[native-session protocol](../docs/resource-sequences.md) for current evaluation.

**Safety/result gate:** PASS

Actual wall-clock timings include asynchronous tracker polling. Discovery received complete preserved frames; annotations were used only for scoring.

| Source | Status | Startup | Recovery | Hot p50 | Hot p95 | Hot max | Wrong accepted poll | First moved unknown | Revision changed | Exact final |
|---|---|---:|---:|---:|---:|---:|:---:|:---:|:---:|:---:|
| `user-paulistinha-20261013` | `ok` | 8659.9 ms | 8706.2 ms | 4.37 ms | 5.20 ms | 5.89 ms | no | yes | yes | yes |
| `dfg-kalibra-ek362` | `ok` | 3020.1 ms | 3046.7 ms | 7.39 ms | 7.71 ms | 7.81 ms | no | yes | yes | yes |
| `dfg-retro-pvp-rp535` | `ok` | 9793.1 ms | 9665.7 ms | 7.44 ms | 12.26 ms | 12.51 ms | no | yes | yes | yes |
| `dfg-pvp-ek133` | `ok` | 2623.3 ms | 2600.3 ms | 8.94 ms | 11.15 ms | 12.07 ms | no | yes | yes | yes |
| `dfg-ustebra-ek177` | `ok` | 8219.2 ms | 8214.9 ms | 7.49 ms | 7.77 ms | 8.09 ms | no | yes | yes | yes |
| `dfg-yonabra-ek195` | `ok` | 4038.7 ms | 3911.4 ms | 21.39 ms | 22.00 ms | 22.81 ms | no | yes | yes | yes |

Errors: **0**.

Startup p50/max: **6128.9 ms/9793.1 ms**.

Recovery p50/max: **6063.1 ms/9665.7 ms**.

Hot reads p50/p95/max: **7.59 ms/21.57 ms/22.81 ms**.
