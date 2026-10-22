### S1-degraded-B: 30 seeds, 150 completed runs

| metric | aimd | aorts | exp-backoff-jitter | fixed-budget | static-polling |
|---|---|---|---|---|---|
| resolution_mean_s | 2.09 [1.84, 2.33] | 3.12 [2.85, 3.39] | 1.3 [1.21, 1.4] | 1.16 [1.07, 1.26] | 1.75 [1.53, 1.98] |
| resolution_p95_s | 3.93 [3.48, 4.38] | 8.09 [7.42, 8.74] | 3.17 [2.74, 3.63] | 2.3 [2.08, 2.55] | 4.19 [3.48, 4.95] |
| backlog_area | 71.5 [60.5, 83.3] | 105 [91.9, 120] | 44.2 [39.7, 49] | 39.4 [34.6, 44.9] | 60.1 [50.7, 69.9] |
| backlog_peak | 11.6 [10.2, 13] | 15.1 [13.8, 16.4] | 8.33 [7.53, 9.17] | 7.7 [6.97, 8.47] | 10.4 [9.1, 11.7] |
| status_check_amplification | 1.9 [1.84, 1.96] | 1.89 [1.82, 1.96] | 2.17 [2.09, 2.25] | 2.08 [2, 2.15] | 1.99 [1.91, 2.07] |
| deadline_compliance | 1 [1, 1] | 1 [1, 1] | 1 [1, 1] | 1 [1, 1] | 1 [1, 1] |
| escalated | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] |
| primary_goodput_per_s | 6.82 [6.72, 6.92] | 6.82 [6.72, 6.92] | 6.82 [6.72, 6.92] | 6.83 [6.73, 6.94] | 6.82 [6.73, 6.91] |
| primary_p95_ms | 281 [276, 287] | 287 [278, 297] | 322 [282, 391] | 290 [279, 302] | 293 [282, 305] |
| primary_success_ratio | 0.752 [0.74, 0.766] | 0.753 [0.74, 0.766] | 0.753 [0.74, 0.767] | 0.754 [0.741, 0.768] | 0.753 [0.74, 0.765] |
| recovery_time_s | 0.089 [0.0222, 0.157] | 0.312 [0.141, 0.518] | 0.106 [0.038, 0.189] | 0.072 [0.0209, 0.139] | 0.0713 [0.0203, 0.139] |
| invariant_violations | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] |
| capacity_rel_err_point | - | 6.74 [6.03, 7.43] | - | - | - |
| capacity_rel_err_point_median | - | 4.95 [2.89, 7.1] | - | - | - |
| capacity_low_signed_err_median | - | 3.43 [1.87, 5.07] | - | - | - |
| capacity_low_overestimate_ratio | - | 0.464 [0.416, 0.507] | - | - | - |

**AORTS vs strongest baseline (paired by seed, bootstrap 95% CI of mean difference AORTS - baseline):**

| metric | strongest baseline | AORTS mean | baseline mean | mean diff [95% CI] | pairs | verdict |
|---|---|---|---|---|---|---|
| resolution_mean_s | fixed-budget | 3.12 | 1.16 | 1.96 [1.71, 2.21] | 30 | AORTS worse (95% CI excludes 0) |
| resolution_p95_s | fixed-budget | 8.09 | 2.3 | 5.79 [5.15, 6.42] | 30 | AORTS worse (95% CI excludes 0) |
| backlog_area | fixed-budget | 105 | 39.4 | 65.9 [55.6, 76.9] | 30 | AORTS worse (95% CI excludes 0) |
| backlog_peak | fixed-budget | 15.1 | 7.7 | 7.4 [6.53, 8.33] | 30 | AORTS worse (95% CI excludes 0) |
| status_check_amplification | aimd | 1.89 | 1.9 | -0.0118 [-0.1, 0.0746] | 30 | no clear difference (95% CI includes 0) |
| deadline_compliance | aimd | 1 | 1 | 0 [0, 0] | 30 | no clear difference (95% CI includes 0) |
| escalated | aimd | 0 | 0 | 0 [0, 0] | 30 | no clear difference (95% CI includes 0) |
| primary_goodput_per_s | fixed-budget | 6.82 | 6.83 | -0.0074 [-0.0426, 0.0296] | 30 | no clear difference (95% CI includes 0) |
| primary_p95_ms | aimd | 287 | 281 | 5.73 [-3.17, 15.9] | 30 | no clear difference (95% CI includes 0) |
| primary_success_ratio | fixed-budget | 0.753 | 0.754 | -0.0008 [-0.0048, 0.0035] | 30 | no clear difference (95% CI includes 0) |
| recovery_time_s | static-polling | 0.312 | 0.0713 | 0.241 [0.0519, 0.463] | 30 | AORTS worse (95% CI excludes 0) |
| invariant_violations | aimd | 0 | 0 | 0 [0, 0] | 30 | no clear difference (95% CI includes 0) |

### S2-degraded-B-flood-poison: 30 seeds, 152 completed runs

| metric | aimd | aorts | exp-backoff-jitter | fixed-budget | static-polling |
|---|---|---|---|---|---|
| resolution_mean_s | 3.53 [2.68, 4.51] | 4.52 [3.62, 5.55] | 58.2 [2.28, 169] | 3.01 [2.03, 4.15] | 10.3 [2.45, 25.1] |
| resolution_p95_s | 6.5 [5.11, 8.07] | 9.91 [8.74, 11.2] | 282 [5.05, 835] | 5.48 [3.88, 7.31] | 14.5 [4.94, 32.3] |
| backlog_area | 101 [78.8, 125] | 140 [116, 166] | 641 [72.5, 1.76e+03] | 95.3 [66.7, 128] | 485 [89.4, 1.08e+03] |
| backlog_peak | 13.7 [12, 15.5] | 16.2 [14.5, 18.1] | 12.1 [10.6, 13.7] | 11.3 [9.63, 13.1] | 11.9 [10.4, 13.4] |
| status_check_amplification | 1.83 [1.77, 1.88] | 1.8 [1.75, 1.86] | 2 [1.91, 2.09] | 1.94 [1.85, 2.03] | 1.9 [1.76, 2.02] |
| deadline_compliance | 1 [1, 1] | 1 [1, 1] | 0.993 [0.979, 1] | 1 [1, 1] | 0.967 [0.9, 1] |
| escalated | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] |
| primary_goodput_per_s | 5.93 [5.23, 6.54] | 5.94 [5.21, 6.58] | 5.97 [5.27, 6.57] | 5.91 [5.16, 6.56] | 5.87 [5.15, 6.53] |
| primary_p95_ms | 2.55e+03 [1.08e+03, 4.33e+03] | 2.26e+03 [730, 3.98e+03] | 2.41e+03 [848, 4.12e+03] | 2.41e+03 [796, 4.15e+03] | 1.92e+03 [694, 3.48e+03] |
| primary_success_ratio | 0.76 [0.745, 0.774] | 0.753 [0.737, 0.769] | 0.747 [0.731, 0.762] | 0.747 [0.731, 0.762] | 0.746 [0.725, 0.767] |
| recovery_time_s | 0.0908 [0.0253, 0.159] | 0.521 [0.226, 0.88] | 0.0243 [0.0057, 0.0599] | 0.0728 [0.0216, 0.14] | 0.0997 [0.0102, 0.235] |
| invariant_violations | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] |
| capacity_rel_err_point | - | 6.36 [5.57, 7.18] | - | - | - |
| capacity_rel_err_point_median | - | 4.97 [3.05, 7.1] | - | - | - |
| capacity_low_signed_err_median | - | 2.55 [1.36, 3.88] | - | - | - |
| capacity_low_overestimate_ratio | - | 0.531 [0.454, 0.613] | - | - | - |

**AORTS vs strongest baseline (paired by seed, bootstrap 95% CI of mean difference AORTS - baseline):**

| metric | strongest baseline | AORTS mean | baseline mean | mean diff [95% CI] | pairs | verdict |
|---|---|---|---|---|---|---|
| resolution_mean_s | fixed-budget | 4.52 | 3.01 | 1.51 [0.79, 2.12] | 30 | AORTS worse (95% CI excludes 0) |
| resolution_p95_s | fixed-budget | 9.91 | 5.48 | 4.43 [3.07, 5.62] | 30 | AORTS worse (95% CI excludes 0) |
| backlog_area | fixed-budget | 140 | 95.3 | 44.9 [21.8, 65.5] | 30 | AORTS worse (95% CI excludes 0) |
| backlog_peak | fixed-budget | 16.2 | 11.3 | 4.93 [3.07, 6.8] | 30 | AORTS worse (95% CI excludes 0) |
| status_check_amplification | aimd | 1.8 | 1.83 | -0.0261 [-0.0927, 0.0401] | 30 | no clear difference (95% CI includes 0) |
| deadline_compliance | aimd | 1 | 1 | 0 [0, 0] | 30 | no clear difference (95% CI includes 0) |
| escalated | aimd | 0 | 0 | 0 [0, 0] | 30 | no clear difference (95% CI includes 0) |
| primary_goodput_per_s | exp-backoff-jitter | 5.94 | 5.97 | -0.046 [-0.632, 0.494] | 29 | no clear difference (95% CI includes 0) |
| primary_p95_ms | static-polling | 2.26e+03 | 1.92e+03 | 335 [-1.29e+03, 2.04e+03] | 30 | no clear difference (95% CI includes 0) |
| primary_success_ratio | aimd | 0.753 | 0.76 | -0.0068 [-0.0183, 0.0023] | 30 | no clear difference (95% CI includes 0) |
| recovery_time_s | exp-backoff-jitter | 0.521 | 0.0243 | 0.426 [0.145, 0.762] | 29 | AORTS worse (95% CI excludes 0) |
| invariant_violations | aimd | 0 | 0 | 0 [0, 0] | 30 | no clear difference (95% CI includes 0) |

### S3-heldout-relapse (held-out): 30 seeds, 150 completed runs

| metric | aimd | aorts | exp-backoff-jitter | fixed-budget | static-polling |
|---|---|---|---|---|---|
| resolution_mean_s | 3.07 [2.74, 3.44] | 4.61 [4.24, 4.99] | 23.3 [2.21, 65.1] | 2.61 [2.33, 2.95] | 3.06 [2.76, 3.4] |
| resolution_p95_s | 5.59 [4.92, 6.36] | 10.6 [9.86, 11.3] | 147 [4.47, 432] | 4.64 [4.14, 5.25] | 6.04 [5.49, 6.64] |
| backlog_area | 192 [168, 216] | 285 [252, 321] | 2.28e+03 [133, 6.57e+03] | 157 [138, 178] | 404 [167, 862] |
| backlog_peak | 20.3 [18.4, 22.1] | 23.6 [20.8, 26.5] | 19.1 [17.4, 20.7] | 19.2 [17.7, 20.7] | 20.5 [18.6, 22.5] |
| status_check_amplification | 1.47 [1.44, 1.5] | 1.4 [1.37, 1.44] | 1.58 [1.55, 1.62] | 1.54 [1.5, 1.58] | 1.48 [1.45, 1.52] |
| deadline_compliance | 1 [1, 1] | 1 [1, 1] | 0.995 [0.985, 1] | 1 [1, 1] | 1 [1, 1] |
| escalated | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] |
| primary_goodput_per_s | 9.54 [8.8, 10.2] | 9.56 [8.84, 10.2] | 9.67 [8.96, 10.2] | 9.83 [9.13, 10.3] | 9.48 [8.61, 10.2] |
| primary_p95_ms | 1.39e+03 [462, 2.48e+03] | 1.48e+03 [453, 2.67e+03] | 1.04e+03 [419, 1.97e+03] | 1.08e+03 [419, 2.08e+03] | 1.38e+03 [436, 2.4e+03] |
| primary_success_ratio | 0.745 [0.727, 0.766] | 0.746 [0.727, 0.767] | 0.737 [0.716, 0.757] | 0.747 [0.732, 0.763] | 0.728 [0.688, 0.757] |
| recovery_time_s | 0.106 [0.0374, 0.207] | 4.06 [3.3, 4.8] | 0.149 [0.0202, 0.371] | 0.107 [0.039, 0.175] | 0.0553 [0.0049, 0.122] |
| invariant_violations | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] | 0 [0, 0] |
| capacity_rel_err_point | - | 3.7 [3.2, 4.19] | - | - | - |
| capacity_rel_err_point_median | - | 1.26 [1.03, 1.56] | - | - | - |
| capacity_low_signed_err_median | - | 0.127 [-0.0146, 0.314] | - | - | - |
| capacity_low_overestimate_ratio | - | 0.447 [0.366, 0.536] | - | - | - |

**AORTS vs strongest baseline (paired by seed, bootstrap 95% CI of mean difference AORTS - baseline):**

| metric | strongest baseline | AORTS mean | baseline mean | mean diff [95% CI] | pairs | verdict |
|---|---|---|---|---|---|---|
| resolution_mean_s | fixed-budget | 4.61 | 2.61 | 2 [1.59, 2.4] | 30 | AORTS worse (95% CI excludes 0) |
| resolution_p95_s | fixed-budget | 10.6 | 4.64 | 5.92 [5, 6.79] | 30 | AORTS worse (95% CI excludes 0) |
| backlog_area | fixed-budget | 285 | 157 | 128 [97.3, 161] | 30 | AORTS worse (95% CI excludes 0) |
| backlog_peak | exp-backoff-jitter | 23.6 | 19.1 | 4.5 [2.27, 6.8] | 30 | AORTS worse (95% CI excludes 0) |
| status_check_amplification | aimd | 1.4 | 1.47 | -0.0683 [-0.112, -0.0201] | 30 | AORTS better (95% CI excludes 0) |
| deadline_compliance | aimd | 1 | 1 | 0 [0, 0] | 30 | no clear difference (95% CI includes 0) |
| escalated | aimd | 0 | 0 | 0 [0, 0] | 30 | no clear difference (95% CI includes 0) |
| primary_goodput_per_s | fixed-budget | 9.56 | 9.83 | -0.267 [-0.842, 0.0833] | 30 | no clear difference (95% CI includes 0) |
| primary_p95_ms | exp-backoff-jitter | 1.48e+03 | 1.04e+03 | 435 [-22, 1.27e+03] | 30 | no clear difference (95% CI includes 0) |
| primary_success_ratio | fixed-budget | 0.746 | 0.747 | -0.0009 [-0.0121, 0.0107] | 30 | no clear difference (95% CI includes 0) |
| recovery_time_s | static-polling | 4.06 | 0.0553 | 4 [3.25, 4.76] | 30 | AORTS worse (95% CI excludes 0) |
| invariant_violations | aimd | 0 | 0 | 0 [0, 0] | 30 | no clear difference (95% CI includes 0) |

Failed/aborted runs excluded: 6 (['RuntimeError: dispatcher did not pick up the new controller (is the experiment stack running?)', 'RuntimeError: dispatcher did not pick up the new controller (is the experiment stack running?)', 'RuntimeError: dispatcher did not pick up the new controller (is the experiment stack running?)', '3 consecutive failed runs; stack unhealthy', 'RemoteProtocolError: Server disconnected without sending a response.'])