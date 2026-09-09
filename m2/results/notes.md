# M2 timeline and resource notes

All load-stage timestamps are UTC and are recorded more precisely in
`timeline.jsonl` and `summary.csv`. The load generator, API-process sampler, and
screenshots used the same workstation clock.

## Hosted series H

Public URL: <https://campus-pulse-csce553.onrender.com>

- 06:30:40--06:31:12: after 15 minutes with no intervening requests,
  `GET /health` returned HTTP 200 in 32.624475 s (15-byte response). This is the
  required one-off cold-start measurement and is kept separate from the warm
  steady-state percentiles. Raw evidence: `H-cold-start.txt`.

- 03:52:51--03:53:51: warm `/health`, c=2.
- 03:54:31--04:02:27: authenticated read staircase c=1, 4, 8, 16, 32.
- At c=32, p99=8384.65 ms triggered the 5-second stop rule.
- 04:03:09--04:04:11: repeated c=16, the stage below the stop.
- 04:04:52--04:05:53: write stage at c=16.
- 04:06:34--04:07:37: 80/20 read/write mix at c=16.
- `hosted-url.png` was captured at 03:54:42; `hosted-url.json` records the URL,
  time, and capture tool.

## Local series L — same Supabase PostgreSQL

The local API used the same Supabase PostgreSQL 17.6 database as hosted H.
Connectivity and the PostgreSQL dialect were verified before the run without
printing the connection secret. The series ran from 06:48--07:03 UTC.

The API process was sampled every five seconds in `L-api-resources.csv`.

| Stage | Mean API CPU (% of one core) | Max CPU | Max RSS (MiB) |
|---|---:|---:|---:|
| L-health-c2 | 101.8 | 102.4 | 86.45 |
| L-read-c1 | 7.0 | 7.4 | 86.45 |
| L-read-c4 | 24.6 | 25.8 | 87.95 |
| L-read-c8 | 52.0 | 54.6 | 89.70 |
| L-read-c16 | 80.6 | 84.6 | 93.20 |
| L-read-c32 | 79.9 | 82.8 | 97.20 |
| L-read-c32-repeat | 79.9 | 83.0 | 98.20 |
| L-write-c32 | 19.3 | 20.8 | 99.70 |
| L-mix-c32 | 63.2 | 67.4 | 100.93 |

CPU rose with read concurrency and plateaued near 80% at c=16--32. Read
throughput likewise flattened near 31.2 RPS while p99 rose from 724.58 to
1724.79 ms. RSS rose gradually from 86.45 to 98.20 MiB across the read sweep.
The same database makes this series comparable to H, while laptop-to-pooler
network latency remains a documented confound.

`local-url.png` and `local-url.json` record the local UI evidence.
