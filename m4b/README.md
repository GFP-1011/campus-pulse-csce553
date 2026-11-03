# M4b — Include Indexes

Index experiment on the M4 "million" workload (20k users, 200k posts, 1M likes, 200k friendships),
run directly against Supabase PostgreSQL. Report: [m4b-report.md](m4b-report.md).

| File | Purpose |
|---|---|
| `m4b.py` | Builds schema `m4b` (primary keys only), runs EXPLAIN ANALYZE + load tests per index configuration, writes `results/` |
| `dashboard_explain.sql` | Before/after EXPLAIN statements for the Supabase SQL Editor |
| `results/summary.md` | Generated tables |
| `results/explain.csv`, `results/load.csv` | Raw measurements |
| `results/plans/` | Every EXPLAIN plan (`<config>__<query>.txt/.json`) |

```bash
pip install "psycopg[binary]"
export DATABASE_URL='postgresql://postgres.<ref>:<password>@<pooler-host>:5432/postgres'   # Session pooler
python m4b.py setup                                             # data, no secondary indexes
python m4b.py bench --duration 10 --configs none,A,ABCD,ABCDEF  # resumable
python m4b.py drop                                              # remove schema afterwards
```

Index codes: A `likes(post_id)`, B `posts(author_id, id DESC)`, C `likes(user_id, id DESC)`,
D `friendships(user_id)`, E `users(email)`, F `posts(created_at DESC)`. Recommended set: A+B+C+D.
