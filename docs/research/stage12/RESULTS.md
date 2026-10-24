# Stage 12 results (generated; do not edit by hand)

- source digest `fef8b37a5c813f34b153a8a06fb9776a1894d3f3e066a3c76650448de356d698`; design digest `81327546ec98c242fb6c88768b5fcb013b6fad904777481761d126a3a398aba6`
- runs used 2100; manifest status {'completed': 2100}; rows from other source digests excluded: 0
- engine: independent SimPy DES for every arm; scale 0.02; paired seeds per scenario as listed
- cells: mean paired difference arm - reference (median), [95 % bootstrap CI of the mean] when n >= 10; negative is better for new ambiguous / unresolved / latency

## normal_load

healthy route; ambiguity is rare, so checks have little legitimate benefit. Paired seeds: 10.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 0.0 | 0.0 | 0.39 / 0.50 | - | 0.0 |
| static-polling | 0.0 | 0.0 | 0.0 | 0.0 | 0.39 / 0.50 | - | 0.0 |
| exp-backoff-jitter | 0.0 | 0.0 | 0.0 | 0.0 | 0.39 / 0.50 | - | 0.0 |
| fixed-budget | 0.0 | 0.0 | 0.0 | 0.0 | 0.39 / 0.50 | - | 0.0 |
| aimd | 0.0 | 0.0 | 0.0 | 0.0 | 0.39 / 0.50 | - | 0.0 |
| aorts | 0.0 | 0.0 | 0.0 | 0.0 | 0.39 / 0.50 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| exp-backoff-jitter | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| fixed-budget | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aimd | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |

## near_saturation

rho 0.95; extra checks consume scarce capacity. Paired seeds: 10.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 0.0 | 0.0 | 1.83 / 2.53 | - | 0.0 |
| static-polling | 0.0 | 0.0 | 0.0 | 0.0 | 1.83 / 2.53 | - | 0.0 |
| exp-backoff-jitter | 0.0 | 0.0 | 0.0 | 0.0 | 1.83 / 2.53 | - | 0.0 |
| fixed-budget | 0.0 | 0.0 | 0.0 | 0.0 | 1.83 / 2.53 | - | 0.0 |
| aimd | 0.0 | 0.0 | 0.0 | 0.0 | 1.83 / 2.53 | - | 0.0 |
| aorts | 0.0 | 0.0 | 0.0 | 0.0 | 1.83 / 2.53 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| exp-backoff-jitter | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| fixed-budget | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aimd | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |

## sudden_degradation

capacity -50 % at 595 s until the end; unannounced drop (estimator detection lag). Paired seeds: 30.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 12641.7 | 4099.5 | 2.10 / 12.22 | - | 0.0 |
| static-polling | 6892.0 | 0.0 | 12682.4 | 4879.3 | 2.10 / 12.22 | - | 0.0 |
| exp-backoff-jitter | 6455.1 | 0.0 | 12682.3 | 4881.0 | 2.10 / 12.22 | - | 0.0 |
| fixed-budget | 2220.8 | 0.0 | 12681.4 | 4861.6 | 2.10 / 12.22 | - | 0.0 |
| aimd | 791.6 | 0.0 | 12667.5 | 4598.4 | 2.10 / 12.22 | - | 0.0 |
| aorts | 0.0 | 0.0 | 12641.7 | 4099.5 | 2.10 / 12.22 | - | 0.0 |
| aorts-no-feedback | 0.0 | 0.0 | 12641.7 | 4099.5 | 2.10 / 12.22 | - | 0.0 |
| aorts-no-lookahead | 0.0 | 0.0 | 12641.7 | 4099.5 | 2.10 / 12.22 | - | 0.0 |
| aorts-no-uncertainty | 0.0 | 0.0 | 12641.7 | 4099.5 | 2.10 / 12.22 | - | 0.0 |
| aorts-no-deadline | 0.0 | 0.0 | 12641.7 | 4099.5 | 2.10 / 12.22 | - | 0.0 |
| aorts-fixed | 2220.8 | 0.0 | 12681.4 | 4861.6 | 2.10 / 12.22 | - | 0.0 |
| aorts@ambiguity_averse | 0.0 | 0.0 | 12641.7 | 4099.5 | 2.10 / 12.22 | - | 0.0 |
| aorts@recovery_seeking | 0.0 | 0.0 | 12641.7 | 4099.5 | 2.10 / 12.22 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +40.8 (med +38.0) [+36.2, +45.6] | +0.0 (med +0.0) | +779.9 (med +693.0) [+706.0, +858.4] |
| exp-backoff-jitter | +40.7 (med +38.0) [+36.1, +45.6] | +0.0 (med +0.0) | +781.5 (med +692.0) [+707.7, +860.1] |
| fixed-budget | +39.7 (med +36.0) [+35.3, +44.5] | +0.0 (med +0.0) | +762.1 (med +680.0) [+690.9, +837.7] |
| aimd | +25.8 (med +23.5) [+23.0, +28.8] | +0.0 (med +0.0) | +498.9 (med +449.5) [+453.7, +547.1] |
| aorts | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-feedback | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-lookahead | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-uncertainty | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-deadline | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-fixed | +39.7 (med +36.0) [+35.3, +44.5] | +0.0 (med +0.0) | +762.1 (med +680.0) [+690.9, +837.7] |
| aorts@ambiguity_averse | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts@recovery_seeking | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |

## prolonged_fault

report S2: capacity -35 % for 20 minutes; Stage 05 found checks counterproductive. Paired seeds: 30.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 15143.0 | 1397.2 | 10.65 / 14.03 | - | 0.0 |
| static-polling | 9306.2 | 0.0 | 21298.7 | 3118.3 | 12.05 / 14.31 | - | 0.0 |
| exp-backoff-jitter | 9128.8 | 0.0 | 22677.3 | 3193.8 | 11.97 / 14.28 | - | 0.0 |
| fixed-budget | 3930.1 | 0.0 | 16132.5 | 2645.3 | 11.67 / 14.27 | - | 0.0 |
| aimd | 1384.7 | 0.0 | 15372.4 | 2048.5 | 10.98 / 14.10 | - | 0.0 |
| aorts | 5.4 | 0.0 | 15143.0 | 1397.2 | 10.65 / 14.03 | - | 0.0 |
| aorts-no-feedback | 5.4 | 0.0 | 15143.0 | 1397.2 | 10.65 / 14.03 | - | 0.0 |
| aorts-no-lookahead | 725.8 | 0.0 | 15143.0 | 1397.2 | 10.65 / 14.03 | - | 0.0 |
| aorts-no-uncertainty | 24.3 | 0.0 | 15143.0 | 1397.2 | 10.65 / 14.03 | - | 0.0 |
| aorts-no-deadline | 5.4 | 0.0 | 15143.0 | 1397.2 | 10.65 / 14.03 | - | 0.0 |
| aorts-fixed | 3926.5 | 0.0 | 16132.5 | 2645.3 | 11.67 / 14.27 | - | 0.0 |
| aorts@ambiguity_averse | 0.0 | 0.0 | 15143.0 | 1397.2 | 10.65 / 14.03 | - | 0.0 |
| aorts@recovery_seeking | 5.4 | 0.0 | 15143.0 | 1397.2 | 10.65 / 14.03 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +6155.8 (med +6226.5) [+5721.9, +6592.0] | +0.0 (med +0.0) | +1721.1 (med +1734.5) [+1596.1, +1850.4] |
| exp-backoff-jitter | +7534.3 (med +7945.0) [+7233.3, +7812.1] | +0.0 (med +0.0) | +1796.6 (med +1782.0) [+1653.2, +1944.0] |
| fixed-budget | +989.5 (med +963.0) [+916.1, +1065.3] | +0.0 (med +0.0) | +1248.1 (med +1269.0) [+1183.6, +1311.5] |
| aimd | +229.4 (med +238.5) [+211.9, +247.5] | +0.0 (med +0.0) | +651.4 (med +661.0) [+622.9, +679.0] |
| aorts | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-feedback | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-lookahead | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-uncertainty | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-deadline | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-fixed | +989.5 (med +963.0) [+916.1, +1065.3] | +0.0 (med +0.0) | +1248.1 (med +1269.0) [+1183.6, +1311.5] |
| aorts@ambiguity_averse | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts@recovery_seeking | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |

## status_check_bursts

30 % lost confirmations (checks can resolve) plus exogenous A3 check demand at 25 % of capacity for 300 s on the shared route. Paired seeds: 10.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 6399.7 | 6399.7 | 1.45 / 2.31 | - | 0.0 |
| static-polling | 11146.6 | 1166.6 | 16748.6 | 6359.3 | 9.49 / 13.74 | - | 0.0 |
| exp-backoff-jitter | 10757.1 | 1069.6 | 16770.2 | 6464.1 | 9.32 / 13.73 | - | 0.0 |
| fixed-budget | 3926.3 | 2607.3 | 9392.3 | 3860.1 | 11.29 / 14.05 | - | 0.0 |
| aimd | 3959.0 | 3921.1 | 6427.2 | 2475.3 | 7.24 / 8.93 | - | 0.0 |
| aorts | 0.0 | 0.0 | 6399.7 | 6399.7 | 1.45 / 2.31 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +10348.9 (med +10361.5) [+10267.6, +10410.8] | +1166.6 (med +1170.5) [+1141.0, +1192.8] | -40.4 (med -16.0) [-105.4, +14.1] |
| exp-backoff-jitter | +10370.5 (med +10397.5) [+10290.2, +10428.9] | +1069.6 (med +1077.0) [+1044.3, +1094.2] | +64.4 (med +129.0) [-21.9, +135.7] |
| fixed-budget | +2992.6 (med +3065.0) [+2833.0, +3150.2] | +2607.3 (med +2600.5) [+2527.3, +2687.5] | -2539.6 (med -2542.0) [-2644.6, -2434.1] |
| aimd | +27.5 (med -3.0) [-3.7, +89.1] | +3921.1 (med +3955.5) [+3845.1, +3992.8] | -3924.4 (med -3959.0) [-3995.8, -3848.8] |
| aorts | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |

## repay_feedback

every customer re-pays an ambiguous payment; capacity -35 % from 400 s. Paired seeds: 30.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 19975.0 | 8266.9 | 12.21 / 14.22 | - | 0.0 |
| static-polling | 7109.6 | 0.0 | 20229.0 | 9129.3 | 12.21 / 14.22 | - | 0.0 |
| exp-backoff-jitter | 6897.1 | 0.0 | 20227.1 | 9124.6 | 12.21 / 14.22 | - | 0.0 |
| fixed-budget | 2209.4 | 0.0 | 20226.8 | 9121.7 | 12.21 / 14.22 | - | 0.0 |
| aimd | 791.6 | 0.0 | 20168.1 | 8919.5 | 12.21 / 14.22 | - | 0.0 |
| aorts | 0.0 | 0.0 | 19975.0 | 8266.9 | 12.21 / 14.22 | - | 0.0 |
| aorts-no-feedback | 0.0 | 0.0 | 19975.0 | 8266.9 | 12.21 / 14.22 | - | 0.0 |
| aorts-no-lookahead | 0.0 | 0.0 | 19975.0 | 8266.9 | 12.21 / 14.22 | - | 0.0 |
| aorts-no-uncertainty | 0.0 | 0.0 | 19975.0 | 8266.9 | 12.21 / 14.22 | - | 0.0 |
| aorts-no-deadline | 0.0 | 0.0 | 19975.0 | 8266.9 | 12.21 / 14.22 | - | 0.0 |
| aorts-fixed | 2209.4 | 0.0 | 20226.8 | 9121.7 | 12.21 / 14.22 | - | 0.0 |
| aorts@ambiguity_averse | 0.0 | 0.0 | 19975.0 | 8266.9 | 12.21 / 14.22 | - | 0.0 |
| aorts@recovery_seeking | 0.0 | 0.0 | 19975.0 | 8266.9 | 12.21 / 14.22 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +253.9 (med +250.5) [+225.9, +281.7] | +0.0 (med +0.0) | +862.4 (med +871.5) [+763.8, +961.8] |
| exp-backoff-jitter | +252.1 (med +247.0) [+224.0, +279.9] | +0.0 (med +0.0) | +857.7 (med +863.0) [+759.0, +956.6] |
| fixed-budget | +251.7 (med +249.0) [+223.9, +279.2] | +0.0 (med +0.0) | +854.8 (med +860.5) [+757.5, +953.2] |
| aimd | +193.0 (med +202.5) [+171.5, +214.2] | +0.0 (med +0.0) | +652.6 (med +646.5) [+578.1, +727.3] |
| aorts | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-feedback | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-lookahead | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-uncertainty | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-no-deadline | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts-fixed | +251.7 (med +249.0) [+223.9, +279.2] | +0.0 (med +0.0) | +854.8 (med +860.5) [+757.5, +953.2] |
| aorts@ambiguity_averse | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts@recovery_seeking | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |

## lost_confirmation

RV-1: 30 % of confirmations lost and capacity -20 % (300-1200 s); checks can resolve. Paired seeds: 30.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 4172.0 | 4172.0 | 0.88 / 1.40 | - | 0.0 |
| static-polling | 10213.4 | 250.5 | 15785.6 | 5505.8 | 9.07 / 13.64 | - | 0.0 |
| exp-backoff-jitter | 9673.6 | 272.5 | 15528.7 | 5429.1 | 8.83 / 13.67 | - | 0.0 |
| fixed-budget | 3326.4 | 819.9 | 12161.3 | 3775.7 | 12.59 / 14.41 | - | 0.0 |
| aimd | 2278.2 | 2278.2 | 4171.1 | 1892.9 | 2.81 / 3.76 | - | 0.0 |
| aorts | 2.3 | 2.3 | 4172.0 | 4169.6 | 0.88 / 1.40 | - | 0.0 |
| aorts-no-feedback | 2.3 | 2.3 | 4172.0 | 4169.6 | 0.88 / 1.40 | - | 0.0 |
| aorts-no-lookahead | 2.3 | 2.3 | 4172.0 | 4169.6 | 0.88 / 1.40 | - | 0.0 |
| aorts-no-uncertainty | 475.8 | 475.8 | 4172.0 | 3696.2 | 0.89 / 1.40 | - | 0.0 |
| aorts-no-deadline | 2.3 | 2.3 | 4172.0 | 4169.6 | 0.88 / 1.40 | - | 0.0 |
| aorts-fixed | 3325.5 | 819.0 | 12161.3 | 3776.6 | 12.59 / 14.41 | - | 0.0 |
| aorts@ambiguity_averse | 0.0 | 0.0 | 4172.0 | 4172.0 | 0.88 / 1.40 | - | 0.0 |
| aorts@recovery_seeking | 2.3 | 2.3 | 4172.0 | 4169.6 | 0.88 / 1.40 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +11613.6 (med +11641.5) [+11561.4, +11666.8] | +250.5 (med +247.5) [+238.0, +263.4] | +1333.8 (med +1297.5) [+1271.4, +1396.7] |
| exp-backoff-jitter | +11356.8 (med +11358.0) [+11308.1, +11405.1] | +272.5 (med +263.0) [+256.7, +289.3] | +1257.1 (med +1230.5) [+1202.8, +1309.6] |
| fixed-budget | +7989.3 (med +8019.0) [+7830.0, +8140.9] | +819.9 (med +806.5) [+791.0, +849.8] | -396.3 (med -390.0) [-441.6, -352.7] |
| aimd | -0.8 (med -1.0) [-1.2, -0.5] | +2278.2 (med +2275.0) [+2249.7, +2306.0] | -2279.1 (med -2275.5) [-2307.0, -2250.5] |
| aorts | +0.0 (med +0.0) | +2.3 (med +0.0) [+0.6, +4.7] | -2.3 (med +0.0) [-4.7, -0.6] |
| aorts-no-feedback | +0.0 (med +0.0) | +2.3 (med +0.0) [+0.6, +4.7] | -2.3 (med +0.0) [-4.7, -0.6] |
| aorts-no-lookahead | +0.0 (med +0.0) | +2.3 (med +0.0) [+0.6, +4.7] | -2.3 (med +0.0) [-4.7, -0.6] |
| aorts-no-uncertainty | +0.0 (med +0.0) | +475.8 (med +442.5) [+438.3, +517.1] | -475.8 (med -442.5) [-517.1, -438.3] |
| aorts-no-deadline | +0.0 (med +0.0) | +2.3 (med +0.0) [+0.6, +4.7] | -2.3 (med +0.0) [-4.7, -0.6] |
| aorts-fixed | +7989.3 (med +8019.0) [+7830.0, +8140.9] | +819.0 (med +804.5) [+790.3, +849.0] | -395.4 (med -390.0) [-440.5, -352.2] |
| aorts@ambiguity_averse | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts@recovery_seeking | +0.0 (med +0.0) | +2.3 (med +0.0) [+0.6, +4.7] | -2.3 (med +0.0) [-4.7, -0.6] |

## route_recovery

capacity -35 % for 300 s, then a recovered route. Paired seeds: 10.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 1632.8 | 12.5 | 11.07 / 14.07 | - | 0.0 |
| static-polling | 37.5 | 0.0 | 1632.9 | 12.5 | 11.13 / 14.09 | - | 0.0 |
| exp-backoff-jitter | 37.5 | 0.0 | 1632.8 | 12.5 | 11.08 / 14.09 | - | 0.0 |
| fixed-budget | 37.5 | 0.0 | 1632.9 | 12.5 | 11.12 / 14.09 | - | 0.0 |
| aimd | 37.5 | 0.0 | 1632.9 | 12.5 | 11.12 / 14.09 | - | 0.0 |
| aorts | 1.0 | 0.0 | 1632.8 | 12.5 | 11.07 / 14.07 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +0.1 (med +0.0) [+0.0, +0.3] | +0.0 (med +0.0) | +0.0 (med +0.0) |
| exp-backoff-jitter | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |
| fixed-budget | +0.1 (med +0.0) [+0.0, +0.3] | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aimd | +0.1 (med +0.0) [+0.0, +0.3] | +0.0 (med +0.0) | +0.0 (med +0.0) |
| aorts | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |

## healthy_parallel_route

S2 with a healthy A1-C beside the degraded A1-B (route isolation and fairness). Paired seeds: 10.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 15104.4 | 1354.1 | 3.43 / 12.93 | - | 0.0 |
| static-polling | 9096.0 | 0.0 | 21007.6 | 3034.3 | 4.96 / 13.17 | - | 0.0 |
| exp-backoff-jitter | 8965.6 | 0.0 | 22438.5 | 3114.8 | 4.92 / 13.10 | - | 0.0 |
| fixed-budget | 3945.4 | 0.0 | 16033.7 | 2598.0 | 5.49 / 13.18 | - | 0.0 |
| aimd | 1384.6 | 0.0 | 15324.0 | 2000.1 | 3.87 / 13.02 | - | 0.0 |
| aorts | 5.5 | 0.0 | 15104.4 | 1354.1 | 3.43 / 12.93 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +5903.2 (med +6173.0) [+5115.1, +6672.7] | +0.0 (med +0.0) | +1680.2 (med +1663.0) [+1473.2, +1888.8] |
| exp-backoff-jitter | +7334.1 (med +7686.5) [+6697.7, +7912.4] | +0.0 (med +0.0) | +1760.7 (med +1704.5) [+1514.6, +2012.2] |
| fixed-budget | +929.3 (med +901.5) [+818.7, +1057.9] | +0.0 (med +0.0) | +1243.9 (med +1269.0) [+1134.2, +1349.8] |
| aimd | +219.6 (med +214.0) [+190.3, +249.1] | +0.0 (med +0.0) | +646.0 (med +651.0) [+602.6, +688.4] |
| aorts | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |

## stale_telemetry

RV-1 with no telemetry delivered to AORTS for 300 s. Paired seeds: 10. Impairment applies to AORTS arms only.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 4172.4 | 4172.4 | 0.92 / 1.57 | - | 0.0 |
| static-polling | 10215.9 | 250.3 | 15785.2 | 5451.5 | 8.76 / 13.54 | - | 0.0 |
| exp-backoff-jitter | 9689.4 | 260.4 | 15536.8 | 5456.6 | 8.54 / 13.79 | - | 0.0 |
| fixed-budget | 3326.2 | 826.0 | 12167.9 | 3790.2 | 12.41 / 14.40 | - | 0.0 |
| aimd | 2281.4 | 2281.4 | 4171.8 | 1890.4 | 3.55 / 4.48 | - | 0.0 |
| aorts | 1.2 | 1.2 | 4172.4 | 4171.2 | 0.92 / 1.57 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +11612.8 (med +11626.0) [+11543.1, +11681.3] | +250.3 (med +246.0) [+224.8, +278.2] | +1279.1 (med +1242.0) [+1186.2, +1391.2] |
| exp-backoff-jitter | +11364.4 (med +11375.5) [+11279.4, +11437.9] | +260.4 (med +242.0) [+234.2, +292.1] | +1284.2 (med +1257.5) [+1188.6, +1377.2] |
| fixed-budget | +7995.5 (med +8035.0) [+7705.8, +8296.0] | +826.0 (med +816.0) [+775.2, +875.9] | -382.2 (med -376.5) [-443.9, -320.1] |
| aimd | -0.6 (med +0.0) [-1.3, -0.1] | +2281.4 (med +2268.0) [+2244.4, +2325.6] | -2282.0 (med -2270.0) [-2326.0, -2244.9] |
| aorts | +0.0 (med +0.0) | +1.2 (med +0.0) [+0.0, +3.5] | -1.2 (med +0.0) [-3.5, +0.0] |

## poisoned_telemetry

S2 with AORTS's own-traffic throughput reported x2 during the fault (unflagged). Paired seeds: 10. Impairment applies to AORTS arms only.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 15104.4 | 1354.1 | 11.27 / 14.00 | - | 0.0 |
| static-polling | 9096.0 | 0.0 | 21007.6 | 3034.3 | 12.55 / 14.31 | - | 0.0 |
| exp-backoff-jitter | 8965.6 | 0.0 | 22438.5 | 3114.8 | 12.63 / 14.26 | - | 0.0 |
| fixed-budget | 3945.4 | 0.0 | 16033.7 | 2598.0 | 12.15 / 14.31 | - | 0.0 |
| aimd | 1384.6 | 0.0 | 15324.0 | 2000.1 | 11.40 / 14.11 | - | 0.0 |
| aorts | 0.0 | 0.0 | 15104.4 | 1354.1 | 11.27 / 14.00 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +5903.2 (med +6173.0) [+5115.1, +6672.7] | +0.0 (med +0.0) | +1680.2 (med +1663.0) [+1473.2, +1888.8] |
| exp-backoff-jitter | +7334.1 (med +7686.5) [+6697.7, +7912.4] | +0.0 (med +0.0) | +1760.7 (med +1704.5) [+1514.6, +2012.2] |
| fixed-budget | +929.3 (med +901.5) [+818.7, +1057.9] | +0.0 (med +0.0) | +1243.9 (med +1269.0) [+1134.2, +1349.8] |
| aimd | +219.6 (med +214.0) [+190.3, +249.1] | +0.0 (med +0.0) | +646.0 (med +651.0) [+602.6, +688.4] |
| aorts | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |

## poisoned_telemetry_30

S2 with throughput reported x1.3 during the fault (unflagged). Paired seeds: 10. Impairment applies to AORTS arms only.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 15104.4 | 1354.1 | 11.27 / 14.00 | - | 0.0 |
| static-polling | 9096.0 | 0.0 | 21007.6 | 3034.3 | 12.55 / 14.31 | - | 0.0 |
| exp-backoff-jitter | 8965.6 | 0.0 | 22438.5 | 3114.8 | 12.63 / 14.26 | - | 0.0 |
| fixed-budget | 3945.4 | 0.0 | 16033.7 | 2598.0 | 12.15 / 14.31 | - | 0.0 |
| aimd | 1384.6 | 0.0 | 15324.0 | 2000.1 | 11.40 / 14.11 | - | 0.0 |
| aorts | 0.0 | 0.0 | 15104.4 | 1354.1 | 11.27 / 14.00 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +5903.2 (med +6173.0) [+5115.1, +6672.7] | +0.0 (med +0.0) | +1680.2 (med +1663.0) [+1473.2, +1888.8] |
| exp-backoff-jitter | +7334.1 (med +7686.5) [+6697.7, +7912.4] | +0.0 (med +0.0) | +1760.7 (med +1704.5) [+1514.6, +2012.2] |
| fixed-budget | +929.3 (med +901.5) [+818.7, +1057.9] | +0.0 (med +0.0) | +1243.9 (med +1269.0) [+1134.2, +1349.8] |
| aimd | +219.6 (med +214.0) [+190.3, +249.1] | +0.0 (med +0.0) | +646.0 (med +651.0) [+602.6, +688.4] |
| aorts | +0.0 (med +0.0) | +0.0 (med +0.0) | +0.0 (med +0.0) |

## controller_outage

RV-1 with the controller unavailable for 300 s (Track B fallback, 2/route/decision). Paired seeds: 10. Impairment applies to AORTS arms only.

| arm | checks | resolved by check | new ambiguous | unresolved at end | p95 / p99 pay (s) | deadline compl. | safety viol. |
|---|---|---|---|---|---|---|---|
| no-check | 0.0 | 0.0 | 4172.4 | 4172.4 | 0.92 / 1.57 | - | 0.0 |
| static-polling | 10215.9 | 250.3 | 15785.2 | 5451.5 | 8.76 / 13.54 | - | 0.0 |
| exp-backoff-jitter | 9689.4 | 260.4 | 15536.8 | 5456.6 | 8.54 / 13.79 | - | 0.0 |
| fixed-budget | 3326.2 | 826.0 | 12167.9 | 3790.2 | 12.41 / 14.40 | - | 0.0 |
| aimd | 2281.4 | 2281.4 | 4171.8 | 1890.4 | 3.55 / 4.48 | - | 0.0 |
| aorts | 601.1 | 542.5 | 4175.2 | 3629.9 | 7.03 / 8.45 | - | 0.0 |

Paired difference vs no-check (check-induced ambiguity and recovery):

| arm | new ambiguous induced | resolved by check | unresolved at end |
|---|---|---|---|
| static-polling | +11612.8 (med +11626.0) [+11543.1, +11681.3] | +250.3 (med +246.0) [+224.8, +278.2] | +1279.1 (med +1242.0) [+1186.2, +1391.2] |
| exp-backoff-jitter | +11364.4 (med +11375.5) [+11279.4, +11437.9] | +260.4 (med +242.0) [+234.2, +292.1] | +1284.2 (med +1257.5) [+1188.6, +1377.2] |
| fixed-budget | +7995.5 (med +8035.0) [+7705.8, +8296.0] | +826.0 (med +816.0) [+775.2, +875.9] | -382.2 (med -376.5) [-443.9, -320.1] |
| aimd | -0.6 (med +0.0) [-1.3, -0.1] | +2281.4 (med +2268.0) [+2244.4, +2325.6] | -2282.0 (med -2270.0) [-2326.0, -2244.9] |
| aorts | +2.8 (med +0.0) [+0.0, +8.4] | +542.5 (med +600.0) [+473.8, +600.0] | -542.5 (med -600.0) [-600.0, -473.8] |

