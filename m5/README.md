# M5 — Search

Post search for Campus Pulse, built with PostgreSQL full-text search inside Supabase
(following "Recipe for Adding Search"). No new service or secret: the app reuses `DATABASE_URL`.

| Piece | File |
|---|---|
| Search view, indexes, search + facet functions | [`sql/m5_search.sql`](../sql/m5_search.sql) |
| Refresh every 5 minutes (Supabase Cron) | [`sql/m5_cron.sql`](../sql/m5_cron.sql) |
| API: `GET /api/search?q=&tag=&author_id=&since=&sort=relevance\|newest&page=` | [`app/search.py`](../app/search.py) |
| UI: "Search posts" box, hashtag facets, paging | `app/static/` |
| Known-answer tests | [`tests/test_search.py`](../tests/test_search.py) |
| Test data with planned result counts | [`m5/seed_search.py`](seed_search.py) |
| Load test | [`m5/loadtest_search.py`](loadtest_search.py) |

## Search document

| Role | Fields |
|---|---|
| Full-text | post body (weight A), author display name (weight B); `#hashtags` are in the body |
| Filters | hashtag, author, created since |
| Facets | hashtag |
| Sort | relevance (`ts_rank`), newest |
| Display | body, author, time, like count |

Query syntax (`websearch_to_tsquery`): `library exam` (all words), `"study group"` (phrase),
`origami or robotics`, `library -quiet` (exclude). English stemming: `studies` finds `studying`.

## Run it

```bash
# 1. Supabase SQL Editor: run sql/m5_search.sql, enable Integrations -> Cron, run sql/m5_cron.sql
# 2. data + plans
export DATABASE_URL='postgresql://postgres.<ref>:<password>@<pooler-host>:5432/postgres'
python m5/seed_search.py seed      # 2,000 users / 100,000 posts / 200,000 likes
python m5/seed_search.py refresh
python m5/seed_search.py counts    # planned result sizes: 0, 3, 40, 400, 4,000, 40,000
python m5/seed_search.py explain   # m5/results/explain.csv + plans/
# 3. app + load test (second terminal for uvicorn)
uvicorn app.main:app --port 8000
python m5/loadtest_search.py --base http://127.0.0.1:8000 --conc 1,4,8 --duration 15
# 4. tests (PostgreSQL ones need a disposable database)
M5_TEST_DATABASE_URL=postgresql+psycopg://... pytest tests/test_search.py
# cleanup of seeded data:  python m5/seed_search.py clean && python m5/seed_search.py refresh
```
