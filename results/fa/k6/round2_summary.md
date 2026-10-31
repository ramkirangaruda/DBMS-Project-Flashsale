# k6 round 2 from `results/fa/k6/round2.jsonl`

18 runs. 1,000 virtual buyers, 60 units, pessimistic, p_show 0.7, lognormal show delay (median 2 s, sigma 1), one local API process. Invariant (reserved <= total and orders = reserved) held in 18 of 18. Cells: median [min, max] over repetitions. `cwp:1.0` is the cold seeding sale whose p_hat and show delays seed the warm arms of the same repetition.

| arm | n | confirmed | futile: admitted, sold out at checkout | told sold out in the queue | no-show (admitted) | other failures | winner admission-to-verdict p50, ms | admitted-loser admission-to-verdict p50, ms | gate wait p50, ms | gate wait p95, ms | handler (DB) p50, ms | checkout p95 (client), ms | queue-loser verdict p50, ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cwp:1.0 | 3 | 60 [60, 60] | 3 [0, 9] | 916 [902, 921] | 19 [19, 29] | 0 [0, 0] | 5215 [4918, 5354] | 5693 [4598, 6788] | 73 [72, 77] | 163 [160, 213] | 1301 [696, 1603] | 4151 [3700, 4551] | 41349 [38514, 45126] |
| S-K:40 | 3 | 60 [60, 60] | 588 [577, 643] | 0 [0, 0] | 268 [254, 277] | 0 [0, 0] | 5701 [5602, 6824] | 5089 [4405, 5550] | 0 [0, 0] | 0 [0, 0] | 1900 [1472, 2362] | 5045 [4842, 5960] | n/a |
| aimd | 3 | 60 [60, 60] | 616 [601, 644] | 0 [0, 0] | 312 [282, 324] | 0 [0, 0] | 1726 [1565, 2229] | 2636 [2303, 3104] | 0 [0, 0] | 0 [0, 0] | 17 [15, 24] | 3158 [2344, 3171] | n/a |
| cw-censored | 3 | 60 [60, 60] | 3 [1, 8] | 912 [911, 916] | 23 [21, 25] | 0 [0, 0] | 5692 [2294, 5932] | 8885 [4488, 12019] | 87 [45, 121] | 189 [146, 1910] | 869 [332, 1764] | 4470 [3672, 4477] | 42529 [40242, 70860] |
| cwp:1.25:warm:pace | 3 | 60 [60, 60] | 2 [1, 3] | 905 [896, 906] | 31 [29, 33] | 0 [0, 0] | 5104 [4501, 5834] | 12974 [9986, 15648] | 103 [2, 119] | 160 [155, 681] | 583 [26, 638] | 3718 [3249, 4527] | 15167 [10761, 16099] |
| cwp:1.25:warmg:pace | 3 | 60 [60, 60] | 33 [15, 96] | 863 [751, 874] | 37 [29, 64] | 0 [0, 0] | 5398 [4562, 6450] | 9885 [5001, 10911] | 42 [10, 4809] | 311 [160, 6197] | 132 [23, 318] | 4036 [3482, 7729] | 11302 [10267, 16521] |

Other-failure kinds over all runs: none
