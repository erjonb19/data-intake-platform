# Live eval: drift against the recorded baseline

Baseline: the committed recordings, replayed (scripts/live_eval.py baseline). Every input synthetic.

| Agent | Case | Recorded | Live | Turns (rec / live) | Cost (rec / live) | Live latency p50 | Drift |
|---|---|---|---|---|---|---|---|
| incident | platform_oomkilled | pass | pass | 6 / 5 | $0.0043 / $0.0039 | 2.1 s |  |
| incident | schema_drift_hands_off | pass | pass | 9 / 9 | $0.0070 / $0.0068 | 1.4 s |  |
| triage | missing_column | pass | pass | 4 / 4 | $0.0023 / $0.0022 | 1.1 s |  |
| triage | injection_in_a_cell | pass | pass | 4 / 4 | $0.0023 / $0.0022 | 1.3 s |  |
| contract_author | drift_new_plan_code | pass | pass | 10 / 10 | $0.0076 / $0.0074 | 1.7 s |  |
| contract_author | onboard_lab_results | pass | pass | 10 / 10 | $0.0085 / $0.0089 | 1.8 s |  |
| reviewer | good_new_plan_code | pass | pass | 3 / 3 | $0.0020 / $0.0021 | 1.5 s |  |
| reviewer | missed_phi | pass | pass | 3 / 3 | $0.0019 / $0.0019 | 1.3 s |  |

| Agent | Cost (recorded / live) | Calls (recorded / live) | Drift |
|---|---|---|---|
| incident | $0.0112 / $0.0107 | 15 / 14 |  |
| triage | $0.0046 / $0.0044 | 8 / 8 |  |
| contract_author | $0.0161 / $0.0163 | 20 / 20 |  |
| reviewer | $0.0040 / $0.0040 | 6 / 6 |  |

Live spend, this run: $0.0354 (metered).

Drift: none
