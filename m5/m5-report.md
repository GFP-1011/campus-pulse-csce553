# M5 — Introduce Search Capability: Campus Pulse

## 1. What users want to search

Campus Pulse is a campus social feed. Users mostly come to find **posts**: "is anyone going to the volleyball game", "library study group tonight", posts by a classmate, or everything tagged `#gameday`. One search document is therefore one post:

| Role | Fields | Notes |
|---|---|---|
| Full-text | post body (weight **A**), author display name (weight **B**) | Hashtags are part of the body, so `gameday` also finds `#gameday` |
| Filters | hashtag, author, created since | Exact `WHERE` conditions |
| Facets | hashtag | Counts for the current text match |
| Sort | relevance (`ts_rank`) or newest | `id` breaks ties so pages are stable |
| Display | body, author, time, like count | Returned 20 per page |

Visibility: every signed-in user sees the same public feed, so the search needs no per-viewer filter. A materialized view ignores row-level security, so if private posts are added later, the visibility rule must go inside the search function.

## 2. Implementation (following "Recipe for Adding Search")

| Recipe step | Campus Pulse implementation | File |
|---|---|---|
| 1 · Document | Table above | — |
| 2 · Denormalize | Materialized view `search.post_search`: `posts ⨝ users ⨝ like counts`, hashtags extracted with `regexp_matches`, `fts = setweight(body,'A') ‖ setweight(author,'B')` | `sql/m5_search.sql` |
| 3 · Index | GIN on `fts` (inverted index), GIN on `tags`, B-tree on `created_at`, unique index on `id` (required for a concurrent refresh) | `sql/m5_search.sql` |
| 4 · Freshness | Supabase Cron runs `REFRESH MATERIALIZED VIEW CONCURRENTLY` every 5 minutes, so new posts become searchable within 5 minutes | `sql/m5_cron.sql` |
| 5 · Query | `search.search_posts(q, author, tag, since, sort, lim, off)`: `websearch_to_tsquery('english', q)` + `@@` + `ts_rank`; also returns `count(*) over ()` as the total | `sql/m5_search.sql` |
| 6 · App | FastAPI `GET /api/search?q=&tag=&author_id=&since=&sort=&page=` with SQLAlchemy `text()` and bound, `CAST`ed parameters; the "Search posts" box in the UI | `app/search.py`, `app/static/*` |
| 7 · Facets | `search.search_tag_facets(q, …)`: hashtag counts before the tag filter is applied; clicking a chip adds `tag=` | `sql/m5_search.sql`, `app/static/app.js` |
| 8 · Secure | Bound parameters only; `q` limited to 1–200 characters, `tag` to `\w+`, page to 1–50 (20 rows per page); the `search` schema is revoked from `anon`/`authenticated` and is not exposed through the Supabase Data API; the endpoint requires login | `app/search.py`, `sql/m5_search.sql` |
| 9 · Test | Known-answer tests (below); EXPLAIN shows `Bitmap Index Scan on post_search_fts` | `tests/test_search.py`, `m5/results/plans/` |

**Query syntax available to users** (from `websearch_to_tsquery`):

- `library exam`: all words must match
- `"study group"`: phrase
- `origami or robotics`: either word
- `library -quiet`: exclude a word

English stemming means `studies` also matches `study` and `studying`.

**Known-answer tests** (`pytest tests/test_search.py`). All 15 tests pass, including the PostgreSQL tests run on a local PostgreSQL 16 database:

| Query | Expected | Shows that |
|---|---|---|
| `library exam` | post 1 only | all words must match |
| `"study group"` | post 1, not "Group study?" | phrase order matters |
| `studies` | posts 1 and 4 | stemming (study, studying) |
| `library -quiet` | post 1 | exclusion works |
| `volleyball or trails` | posts 3 and 5 | `or` works |
| `thibodeaux` | Bob's two posts | joined author name is searchable |
| `zeppelin` | total 0 | empty result |
| `study` + `tag=finals` | post 1 | tag filter works, facet shows `finals (1)` |
| empty, 201 characters, page 0/51, bad tag | HTTP 422 | input validation |
| no token | HTTP 401 | login required |

## 3. Test data with planned result sizes

`m5/seed_search.py` uses COPY to insert synthetic, realistic-looking campus posts into the app's own tables:

- 2,000 users and 100,000 posts, with 80% of posts written by 20% of users
- 200,000 likes
- post bodies drawn from a Zipf-weighted campus vocabulary of 10–28 words, plus 0–2 hashtags each

On top of that, "probe" words were inserted into an exact number of posts, so that test queries return **anything from nothing to many pages**. Together with the existing demo posts, the search view holds 103,347 posts and uses 57 MB including its indexes.

| Case | Query | Matches | Pages of 20 |
|---|---|---:|---:|
| zero | `zeppelin` | 0 | 0 |
| and-2-words | `library robotics` | 2 | 1 |
| tiny | `origami` | 3 | 1 |
| small | `robotics` | 40 | 2 |
| or | `origami or robotics` | 43 | 3 |
| medium / deep-page | `volleyball` (page 1 / page 10) | 400 | 20 |
| author-name | `thibodeaux` | 996 | 50 |
| phrase | `"study group"` | 1,499 | 75 |
| tag-filter | `campus` + `tag=gameday` | 3,605 | 181 |
| exclude | `library -quiet` | 3,926 | 197 |
| large | `library` | 4,000 | 200 |
| huge / newest-sort | `campus` (relevance / newest) | 40,000 | 2,000 |

## 4. Load-test method

- **Path:** the load generator on a MacBook (Lafayette, LA) sends HTTP requests to a local FastAPI app (one Uvicorn worker, SQLAlchemy pool), which queries Supabase PostgreSQL in us-west-2. This is the same setup as M4.
- **Script:** `m5/loadtest_search.py` was extended from the earlier load tests to call `GET /api/search`. It logs in once, then runs each case as a closed loop at C = 1, 4 and 8. Each stage has 5 s of discarded warm-up, 15 s of measurement and a 3 s pause, with a 10 s request timeout. 42 stages ran on 2026-11-03 between 23:13 and 23:29 UTC, with **0 errors**.
- **Request contents:** each request returns page 1 (20 rows), the exact total and the hashtag facets. That is two SQL calls (search and facets) plus the usual authentication reads.
- **DB ms:** this column is the time spent inside the endpoint on those two SQL calls, network round trips to the database included, as reported by the API's `db_ms` field.
- **EXPLAIN exec ms:** the median PostgreSQL execution time of the search SQL on its own, without the network.

## 5. Results

| Case | Matches | EXPLAIN exec ms | p50 ms (C=1) | p95 ms (C=1) | p95 ms (C=4) | p95 ms (C=8) | RPS (C=8) | DB p95 ms (C=8) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| zero | 0 | 0.06 | 526 | 566 | 559 | 557 | 14.8 | 205 |
| and-2-words | 2 | 0.13 | 525 | 530 | 532 | 540 | 14.8 | 206 |
| tiny | 3 | 0.09 | 524 | 534 | 532 | 634 | 13.9 | 218 |
| small | 40 | 0.22 | 524 | 586 | 537 | 534 | 14.9 | 198 |
| or | 43 | 0.25 | 525 | 547 | 532 | 564 | 14.5 | 170 |
| medium | 400 | 1.44 | 524 | 659 | 536 | 600 | 14.7 | 251 |
| deep-page (page 10) | 400 | 1.63 | 524 | 533 | 599 | 605 | 14.8 | 203 |
| author-name | 996 | 3.33 | 524 | 527 | 604 | 531 | 15.0 | 209 |
| phrase | 1,499 | 5.44 | 527 | 891 | 569 | 606 | 14.4 | 220 |
| tag-filter | 3,605 | 19.19 | 620 | 661 | 659 | **1,575** | 8.5 | 888 |
| exclude | 3,926 | 15.62 | 524 | 528 | 604 | 624 | 14.4 | 245 |
| large | 4,000 | 14.53 | 525 | 586 | 593 | 625 | 14.2 | 279 |
| huge | 40,000 | 194.23 | 713 | 852 | 956 | **1,982** | 7.2 | 1,660 |
| newest-sort | 40,000 | 96.06 | 753 | 996 | 822 | **1,255** | 9.2 | 759 |

The full per-stage table, with mean and p99 for every concurrency level, is in `m5/results/summary.md`. The query plans are in `m5/results/plans/`.

## 6. Analysis

**Searches with up to about 4,000 matches cost the same as searching for nothing.** For every case from 0 to 4,000 matches, the C = 1 median is 524–527 ms. The database part is small: EXPLAIN shows execution times from 0.06 ms to 19 ms. Every plan starts with `Bitmap Index Scan on post_search_fts`, which reads only the GIN posting list for the search term (3–10 buffers). Then `Bitmap Heap Scan` reads only the matching rows, and a `top-N heapsort` keeps 20 of them. Nothing is read and thrown away. This is the behavior the inverted index is meant to provide.

**About 525 ms of every request is network round trips, not search work.** Searching for `zeppelin` takes 0.06 ms in PostgreSQL, but the endpoint's `db_ms` is about 160 ms. That is two SQL calls at roughly 80 ms per round trip from Lafayette to Oregon. The rest of the 525 ms comes from more round trips of the same size: SQLAlchemy's connection pre-ping, the session lookup and the user lookup during authentication.

**Large result sets cost real database time.** For `campus` (40,000 matches), the plan:

1. computes `ts_rank` for all 40,000 rows;
2. materializes all 40,000 rows in a `WindowAgg` to produce `count(*) over ()`, which spills to disk (`temp written=2306` blocks, about 60 ms of the 122–194 ms);
3. sorts them only to return 20.

The facet query then unnests the tags of all 40,000 matches again. As a result, the median rises from 525 to 713 ms. The `newest` sort skips `ts_rank` and is about half as expensive in EXPLAIN (96 ms).

**Under concurrency, only the large-result searches degrade.** Small searches scale almost linearly: at C = 8, about 14.8 req/s, which is close to 8 / 0.53 s. Their p95 stays near 600 ms. For `campus`, throughput levels off at 7.2 req/s and the C = 8 p95 rises to 1,982 ms, with 1,660 ms of it inside the database. This means the shared Supabase CPU is now the bottleneck.

`tag-filter` behaves similarly. Its search reads only 3,605 posts, but its **facet** query counts tags over all 40,000 `campus` matches, because facets are computed before the tag filter is applied. That pushes its C = 8 p95 to 1,575 ms.

**Single outliers:** some C = 1 p95 values, for example 891 ms for the phrase search, come from one or two slow requests out of about 27. They are not plan changes; at C = 4 the same query shows a p95 of 569 ms.

## 7. Making search more responsive

Search is the top of the funnel, so these are the changes that would matter most, in order:

1. **Put the app next to the database.** Deploying the web app in the same region as Supabase (us-west-2) would turn each of the roughly 6 round trips from about 80 ms into about 1 ms. The median would drop from about 525 ms to around 20–30 ms for most searches. No SQL change is needed.
2. **Fewer round trips per request.** Disable `pool_pre_ping` (or recycle connections instead), fetch the session and the user in one query, and return the results and the facets from one SQL call.
3. **Stop counting every match.** Count up to a limit (for example `limit 1001`, shown as "1,000+") instead of `count(*) over ()`. This removes the 40,000-row `WindowAgg` and its disk spill. A narrower working set also helps: rank by `id` and `fts`, then fetch `body` only for the final 20 rows.
4. **Make facets cheaper.** Compute facets only on page 1, or cache them for a few minutes for popular terms. Most deep-page and filtered requests do not need them recomputed.
5. **Consider a higher `work_mem`** for the search role, so large result sets sort in memory instead of spilling to disk.

## 8. Limitations

- The data is synthetic. Probe words were placed to produce controlled result sizes, not to mimic real word frequencies.
- Each stage measured 15 s, about 27 requests at C = 1, so a single slow request can move the p95. The p50 values and the EXPLAIN times are the more stable signals.
- Supabase compute is shared, and the Lafayette–Oregon network adds roughly 80 ms per round trip.
- The search view is refreshed every 5 minutes, so a new post can take up to 5 minutes to appear in search. Like counts shown in search results can also be up to 5 minutes old.
- The search feature lives on the `m5-search` branch. The load test exercised it through a local FastAPI app. The Render deployment serves `main` and will include search once the branch is merged.
