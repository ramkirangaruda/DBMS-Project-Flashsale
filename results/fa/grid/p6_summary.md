# Results from `results/fa/grid/p6.jsonl`

40 runs. Invariant (never oversell) held in 40 of 40. Runs that hit the deadline: 30.

Cells show median [95% bootstrap CI of the median] over seeds; n = seeds per cell.

## pessimistic, p_show = 0.7, demand ratio = 10 (2000 buyers, 200 units)

| arm | n | sold % | time to sell out or deadline, s | admitted, lost (sold out at checkout) | admitted | loser verdict p50, s | OCC retries | client checkout p95, ms | server checkout p95, ms |
|---|---|---|---|---|---|---|---|---|---|
| breakwater:40 | 5 | 100.0 [100.0, 100.0] | 18.5 [14.7, 23.6] | 1034 [938, 1165] | 2000 [2000, 2000] | 28.9 [28.6, 31.0] | 0 [0, 0] | 15965 [12729, 23402] | 38 [25, 47] |
| protego:100 | 5 | 100.0 [100.0, 100.0] | 10.1 [9.9, 10.7] | 1197 [1169, 1212] | 2000 [2000, 2000] | 47.2 [45.2, 50.9] | 0 [0, 0] | 39 [39, 50] | 23 [22, 24] |
| protego:250 | 5 | 100.0 [100.0, 100.0] | 10.7 [10.1, 12.8] | 1197 [1147, 1212] | 2000 [2000, 2000] | 47.6 [45.6, 51.0] | 0 [0, 0] | 38 [35, 305] | 22 [18, 23] |
| protego:50 | 5 | 100.0 [100.0, 100.0] | 11.6 [10.2, 12.2] | 1191 [1149, 1210] | 2000 [2000, 2000] | 49.3 [47.6, 51.0] | 0 [0, 0] | 67 [32, 166] | 22 [20, 22] |

## optimistic, p_show = 0.7, demand ratio = 10 (2000 buyers, 200 units)

| arm | n | sold % | time to sell out or deadline, s | admitted, lost (sold out at checkout) | admitted | loser verdict p50, s | OCC retries | client checkout p95, ms | server checkout p95, ms |
|---|---|---|---|---|---|---|---|---|---|
| breakwater:40 | 5 | 100.0 [100.0, 100.0] | 19.7 [18.8, 22.4] | 1080 [1000, 1152] | 2000 [2000, 2000] | 31.0 [27.9, 33.0] | 88 [7, 107] | 20017 [15256, 23728] | 43 [25, 46] |
| protego:100 | 5 | 100.0 [100.0, 100.0] | 10.3 [10.0, 10.6] | 1197 [1169, 1210] | 2000 [2000, 2000] | 47.6 [46.4, 48.9] | 48 [36, 67] | 53 [37, 126] | 24 [21, 30] |
| protego:250 | 5 | 100.0 [100.0, 100.0] | 12.2 [10.3, 12.5] | 1191 [1146, 1212] | 2000 [2000, 2000] | 50.8 [45.5, 51.8] | 45 [16, 67] | 93 [50, 562] | 22 [18, 39] |
| protego:50 | 5 | 100.0 [100.0, 100.0] | 10.3 [10.0, 12.8] | 1197 [1169, 1212] | 2000 [2000, 2000] | 47.3 [45.5, 53.6] | 46 [31, 54] | 58 [35, 381] | 22 [21, 25] |

## Estimator: final p_hat against the true effective show rate

| strategy | p_show | ratio | arm | n | final p_hat | true p |
|---|---|---|---|---|---|---|

## Claim-window arms: time to 95% sold against time to sell out

| strategy | p_show | ratio | arm | n | t free <= 5% of stock, s | t sold out, s |
|---|---|---|---|---|---|---|
