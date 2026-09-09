# M2 submission guide

Main report: [`m2-report.md`](m2-report.md)  
Raw results and evidence: [`results/`](results/)  
Hosted application: <https://campus-pulse-csce553.onrender.com>

## Completed

- Pre-measurement workload worksheet and dated first-limit prediction.
- Hosted control, read staircase at c=1/4/8/16/32, stop-rule row, c=16
  repeat, write stage, and 80/20 mix stage.
- Offered load, throughput, p50/p95/p99/max, errors, raw request CSVs, fixed
  payload size, timestamps, manifests, and exact reproduction commands.
- Required throughput, latency, and error-rate figures.
- Hosted cold-start measurement after a controlled 15-minute idle window.
- Public URL screenshot with separate URL/time metadata.
- Required local API series against the same Supabase PostgreSQL, plus aligned
  API CPU and memory samples.
- Bottleneck analysis, testable SLO, Little's Law check, revised model, and
  10K/1M/100M discussion.
- Automated application tests (`4 passed`).

## Still required before a fully compliant submission

1. Export or screenshot at least two hosted resource signals aligned with the
   2026-09-09 03:52--04:08 UTC hosted run, such as Render API CPU/memory and
   Supabase database CPU/connections. If historical dashboard resolution cannot
   cover that window, rerun H while the dashboards are open.
Do not add tokens, passwords, `.env`, or files from `m2/.private/` to Git.

## Regenerate figures

```bash
source .venv/bin/activate
python m2/plot_m2.py
```
