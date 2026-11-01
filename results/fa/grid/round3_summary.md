# Round 3: weighting isolated, and wrong priors

Pessimistic, demand ratio 10, 2,000 buyers, 200 units. Median [95% bootstrap CI of the median] over seeds 1 to 5. Frontier rows are from `frontier07.jsonl` / `frontier04.jsonl`, round-3 rows from `round3_07.jsonl` / `round3_04.jsonl`. Warm arms in both use the same priors (D27).

## p_show 0.7

### Posterior vs count weighting, same warm-p start and pacing

| | arm | n | sold % | time to sell out, s | futile | admitted |
|---|---|---|---|---|---|---|
| count, margin 1 | `cwc:1.0:warm:pace` | 5 | 100.0 [99.5, 100.0] | 65.3 [37.1, 90.0] | 2 [1, 5] | 283 [274, 300] |
| posterior, margin 1 | `cwp:1.0:warm:pace` | 5 | 100.0 [100.0, 100.0] | 34.4 [33.5, 43.1] | 3 [2, 7] | 285 [275, 300] |
| count, margin 1.25 | `cwc:1.25:warm:pace` | 5 | 100.0 [100.0, 100.0] | 9.4 [9.1, 10.8] | 23 [18, 34] | 313 [311, 326] |
| posterior, margin 1.25 | `cwp:1.25:warm:pace` | 5 | 100.0 [100.0, 100.0] | 9.3 [9.1, 13.8] | 22 [8, 38] | 309 [305, 333] |
| count, margin 1.5 | `cwc:1.5:warm:pace` | 5 | 100.0 [100.0, 100.0] | 7.7 [6.9, 8.4] | 48 [46, 54] | 346 [344, 368] |
| posterior, margin 1.5 | `cwp:1.5:warm:pace` | 5 | 100.0 [100.0, 100.0] | 8.2 [7.9, 8.2] | 41 [41, 58] | 345 [336, 361] |
| count, margin 2 | `cwc:2.0:warm:pace` | 5 | 100.0 [100.0, 100.0] | 6.8 [6.0, 10.2] | 103 [60, 136] | 429 [374, 471] |
| posterior, margin 2 | `cwp:2.0:warm:pace` | 5 | 100.0 [100.0, 100.0] | 7.5 [6.2, 10.0] | 106 [67, 116] | 433 [374, 454] |

### Right prior (from a p 0.7 sale) vs wrong prior (from a p 0.4 sale)

| | arm | n | sold % | time to sell out, s | futile | admitted |
|---|---|---|---|---|---|---|
| warm, margin 1.25, right prior | `cwp:1.25:warm:pace` | 5 | 100.0 [100.0, 100.0] | 9.3 [9.1, 13.8] | 22 [8, 38] | 309 [305, 333] |
| warm, margin 1.25, wrong prior | `cwp:1.25:warm:pace:from04` | 5 | 100.0 [100.0, 100.0] | 6.9 [6.2, 10.9] | 126 [58, 160] | 462 [372, 507] |
| warm, margin 2, right prior | `cwp:2.0:warm:pace` | 5 | 100.0 [100.0, 100.0] | 7.5 [6.2, 10.0] | 106 [67, 116] | 433 [374, 454] |
| warm, margin 2, wrong prior | `cwp:2.0:warm:pace:from04` | 5 | 100.0 [100.0, 100.0] | 7.6 [6.8, 12.6] | 247 [125, 302] | 642 [463, 725] |
| warmg, margin 1.25, right prior | `cwp:1.25:warmg:pace` | 5 | 100.0 [100.0, 100.0] | 15.3 [13.6, 22.2] | 7 [3, 16] | 288 [278, 305] |
| warmg, margin 1.25, wrong prior | `cwp:1.25:warmg:pace:from04` | 5 | 100.0 [100.0, 100.0] | 31.4 [25.4, 32.9] | 3 [1, 4] | 285 [276, 297] |
| warmg, margin 2, right prior | `cwp:2.0:warmg:pace` | 5 | 100.0 [100.0, 100.0] | 9.2 [7.8, 18.0] | 50 [34, 285] | 344 [324, 708] |
| warmg, margin 2, wrong prior | `cwp:2.0:warmg:pace:from04` | 5 | 100.0 [100.0, 100.0] | 7.6 [7.2, 8.4] | 47 [35, 55] | 351 [327, 353] |

## p_show 0.4

### Posterior vs count weighting, same warm-p start and pacing

| | arm | n | sold % | time to sell out, s | futile | admitted |
|---|---|---|---|---|---|---|
| count, margin 1 | `cwc:1.0:warm:pace` | 5 | 100.0 [99.5, 100.0] | 66.3 [31.0, 90.0] | 2 [1, 3] | 493 [466, 536] |
| posterior, margin 1 | `cwp:1.0:warm:pace` | 5 | 100.0 [100.0, 100.0] | 37.9 [35.0, 40.5] | 4 [2, 19] | 503 [498, 545] |
| count, margin 1.25 | `cwc:1.25:warm:pace` | 5 | 100.0 [100.0, 100.0] | 13.2 [9.7, 37.5] | 12 [2, 36] | 552 [513, 565] |
| posterior, margin 1.25 | `cwp:1.25:warm:pace` | 5 | 100.0 [100.0, 100.0] | 17.2 [11.7, 31.4] | 7 [1, 28] | 535 [471, 570] |
| count, margin 1.5 | `cwc:1.5:warm:pace` | 5 | 100.0 [100.0, 100.0] | 8.9 [8.6, 10.7] | 49 [28, 64] | 634 [592, 651] |
| posterior, margin 1.5 | `cwp:1.5:warm:pace` | 5 | 100.0 [100.0, 100.0] | 10.0 [8.6, 20.6] | 47 [5, 65] | 614 [469, 671] |
| count, margin 2 | `cwc:2.0:warm:pace` | 5 | 100.0 [100.0, 100.0] | 12.4 [8.4, 14.0] | 96 [50, 160] | 775 [680, 855] |
| posterior, margin 2 | `cwp:2.0:warm:pace` | 5 | 100.0 [100.0, 100.0] | 13.3 [8.5, 14.2] | 103 [56, 146] | 768 [693, 826] |

### Right prior (from a p 0.4 sale) vs wrong prior (from a p 0.7 sale)

| | arm | n | sold % | time to sell out, s | futile | admitted |
|---|---|---|---|---|---|---|
| warm, margin 1.25, right prior | `cwp:1.25:warm:pace` | 5 | 100.0 [100.0, 100.0] | 17.2 [11.7, 31.4] | 7 [1, 28] | 535 [471, 570] |
| warm, margin 1.25, wrong prior | `cwp:1.25:warm:pace:from07` | 5 | 100.0 [100.0, 100.0] | 35.5 [33.6, 37.3] | 27 [12, 31] | 569 [526, 594] |
| warm, margin 2, right prior | `cwp:2.0:warm:pace` | 5 | 100.0 [100.0, 100.0] | 13.3 [8.5, 14.2] | 103 [56, 146] | 768 [693, 826] |
| warm, margin 2, wrong prior | `cwp:2.0:warm:pace:from07` | 5 | 100.0 [100.0, 100.0] | 31.2 [13.2, 32.9] | 7 [7, 17] | 515 [493, 570] |
| warmg, margin 1.25, right prior | `cwp:1.25:warmg:pace` | 5 | 100.0 [100.0, 100.0] | 32.9 [32.4, 39.9] | 6 [2, 10] | 525 [480, 541] |
| warmg, margin 1.25, wrong prior | `cwp:1.25:warmg:pace:from07` | 5 | 100.0 [100.0, 100.0] | 22.2 [18.1, 30.6] | 2 [1, 13] | 496 [488, 538] |
| warmg, margin 2, right prior | `cwp:2.0:warmg:pace` | 5 | 100.0 [100.0, 100.0] | 21.8 [19.4, 31.9] | 5 [0, 12] | 503 [484, 553] |
| warmg, margin 2, wrong prior | `cwp:2.0:warmg:pace:from07` | 5 | 100.0 [100.0, 100.0] | 14.0 [9.4, 23.1] | 191 [6, 393] | 1004 [508, 1465] |

