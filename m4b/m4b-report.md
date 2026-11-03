# M4b — Include Indexes: Campus Pulse

## Goal and setup

M4b starts from the M4 workload and asks which indexes are needed for the queries to reach an acceptable p95 (≤ 200 ms). In M4, every foreign-key column already had an index, so the M4 numbers did not include a no-index baseline. For M4b, the M4 "million" dataset was rebuilt in an isolated schema `m4b` with **primary keys only**. Foreign-key constraints were kept, but PostgreSQL does not index foreign keys automatically. Candidate indexes were then added step by step, and each configuration was measured.

| Table | Rows | Distribution (seeded generator) |
|---|---:|---|
| users | 20,000 | — |
| posts | 200,000 | ~80% of posts by 20% of authors, 150-day time span |
| likes | 1,000,000 | ~80% of likes on 20% of posts; ~73,600 posts have no likes |
| friendships | 200,000 | 10 outgoing friendships per user |

All rows total 1.42 million, the same scale as the M4 "million" dataset. Without secondary indexes, the schema uses 136 MB.

**Measurement path.** The load generator runs on a MacBook (Lafayette, LA) and connects to Supabase PostgreSQL (us-west-2) through the Session pooler. Each query is sent directly to the database, so the M4 FastAPI layer, the authentication reads and the three-connection application pool are left out. This isolates the effect of each index. The measured network round trip (`SELECT 1`) has a median of **93.9 ms** and a p95 of **167.4 ms**. No request can be faster than this, so the client-side p95 of every query has a floor of about 130–170 ms.

**Procedure.** For each index configuration, the script:

1. creates the indexes and runs `ANALYZE`;
2. for each query, runs `EXPLAIN (ANALYZE, BUFFERS)` with fixed parameters and records the median execution time, the sequential scans in the plan and the rows read or removed;
3. runs a closed-loop load test at C = 1 and C = 4: 3 s of discarded warm-up, then 10 s of measurement. Parameters rotate deterministically and use the same sequence in every configuration. Statements are limited to 10 s, and requests that time out are counted in the latency percentiles.

All configurations were measured in one session on 2026-10-28 (02:09–02:25 UTC). This avoids the different-day confound noted in M4.

## Queries

| Query | Tables | Returns | Description |
|---|---:|---|---|
| user-one | 1 | single | user by primary key |
| user-by-email | 1 | single | user by email (login lookup) |
| post-list | 2 | list | newest 20 posts before a cursor, with authors |
| author-posts | 2 | list | an author's newest 20 posts |
| stats-one | 3 | single | one post with its author and like count |
| stats-list | 3 | list | 20 posts with authors and like counts |
| user-liked | 3 | list | the 20 posts a user liked most recently |
| friends-feed | 4 | list | newest 20 posts by a user's friends, with like counts |
| popular | 3 | list | top 20 posts by likes over all time (global aggregation, from M4) |
| trending-7d | 3 | list | top 20 posts by likes in the last 7 days |

## Candidate indexes

| Code | Index | Intended for |
|---|---|---|
| A | `likes (post_id)` | like counts per post: stats-one, stats-list, friends-feed, trending |
| B | `posts (author_id, id DESC)` | author-posts, friends-feed |
| C | `likes (user_id, id DESC)` | user-liked |
| D | `friendships (user_id)` | friends-feed |
| E | `UNIQUE users (email)` | user-by-email |
| F | `posts (created_at DESC)` | trending-7d |

The configurations were: `none` (primary keys only) → `A` → `ABCD` → `ABCDEF`.

## Step 1 — Queries with p95 above 200 ms (no secondary indexes)

| Query | p95 C=1 (ms) | p95 C=4 (ms) | DB exec (ms) | Seq scans in plan | Rows removed by filter |
|---|---:|---:|---:|---|---:|
| stats-one | **438.2** | **299.9** | 270.9 | likes | 1,000,000 |
| stats-list | **257.0** | **299.6** | 190.8 | likes | (hash join reads all 1,000,000) |
| user-liked | **369.8** | **523.9** | 199.2 | likes (via backward PK scan) | 999,956 |
| friends-feed | **1,719.3** | **3,276.7** | 1,547.7 | friendships, posts, likes | 20,199,870 |
| popular | **10,113.1** (timeouts) | **10,905.4** (timeouts) | 5,968.9 | likes, posts, users | — (full aggregation) |
| trending-7d | **681.8** | **4,645.3** | 463.8 | likes, posts, users | 184,224 |
| author-posts | 193.2 | 143.6 | 20.8 | posts | 199,996 |
| user-by-email | 133.6 | 136.3 | 2.1 | users | 19,999 |
| user-one / post-list | 143–152 | 141–147 | ≤ 0.2 | — | — |

Six of the ten queries exceed 200 ms. EXPLAIN ANALYZE shows the same pattern in each of them: the database reads a large table and throws away almost every row it reads.

- **stats-one:** to count the likes of one post, it runs a `Parallel Seq Scan on likes`. The filter is `post_id = 102204`, and it reports `Rows Removed by Filter: 500000` per worker. All 1,000,000 likes are read to find about 5 matches.
- **friends-feed:** the like-count subquery runs a `Seq Scan on likes` for **each** of the 20 output rows (`loops=20`, 999,994 rows removed each time), which adds up to about 20 million rows read and discarded. Finding the user's 10 friends also scans all 100,000 friendships per worker.
- **user-liked:** without an index on `likes.user_id`, the planner walks the likes primary key backwards and filters out 999,956 rows before it finds the user's 20 likes.
- **author-posts:** scans all 200,000 posts to find one author's posts. That takes 21 ms on the server, which is still under 200 ms end to end.

## Step 2 — Index experiments

### EXPLAIN ANALYZE execution time (median ms)

| Query | none | A | ABCD | ABCDEF |
|---|---:|---:|---:|---:|
| user-one | 0.04 | 0.05 | 0.04 | 0.04 |
| user-by-email | 2.08 | 2.16 | 2.22 | **0.05** |
| post-list | 0.20 | 0.21 | 0.20 | 0.17 |
| author-posts | 20.83 | 28.31 | **0.07** | 0.07 |
| stats-one | 270.95 | **0.10** | 0.09 | 0.09 |
| stats-list | 190.84 | **0.46** | 0.46 | 0.42 |
| user-liked | 199.17 | 187.82 | **0.28** | 0.27 |
| friends-feed | 1,547.71 | 43.42 | **0.58** | 0.57 |
| popular | 5,968.86 | 18,195.73 | 9,070.08 | 10,659.73 |
| trending-7d | 463.77 | 204.56 | 223.36 | 333.22 |

Each of A, B, C and D gave a speed-up of two to three orders of magnitude on the query it targets. With them in place, the plans changed as follows:

- stats-one / stats-list: `Index Only Scan using likes_post_id` touches 2 and 140 rows, down from 1,000,000.
- user-liked: `Index Scan using likes_user_id_id` touches 60 rows.
- friends-feed: uses `friendships_user_id`, `posts_author_id_id` and `likes_post_id` together, touching 290 rows instead of 20.4 million.
- author-posts: reads 4 index entries instead of 200,000 rows.

### Load test p95 (ms) — C = 1

| Query | none | A | ABCD | ABCDEF |
|---|---:|---:|---:|---:|
| user-one | 152.5 | 152.5 | 144.7 | 150.1 |
| user-by-email | 133.6 | 131.6 | 134.5 | 172.9 |
| post-list | 143.4 | 136.0 | 126.5 | 136.9 |
| author-posts | 193.2 | 185.6 | *349.3* | 142.6 |
| stats-one | **438.2** | 133.3 | *218.6* | 169.3 |
| stats-list | **257.0** | 184.6 | 182.7 | 182.3 |
| user-liked | **369.8** | **354.9** | 115.7 | 126.2 |
| friends-feed | **1,719.3** | 145.5 | 174.9 | 180.1 |
| popular | **10,113.1** | **10,112.9** | **6,003.2** | **6,245.6** |
| trending-7d | **681.8** | **665.4** | **983.0** | **712.8** |

### Load test p95 (ms) — C = 4

| Query | none | A | ABCD | ABCDEF |
|---|---:|---:|---:|---:|
| user-one | 146.8 | 149.5 | 142.3 | *395.1* |
| user-by-email | 136.3 | 132.7 | 131.5 | 158.5 |
| post-list | 141.0 | 130.9 | 143.8 | 144.7 |
| author-posts | 143.6 | *207.9* | *263.0* | 143.0 |
| stats-one | **299.9** | 143.6 | *213.6* | 150.8 |
| stats-list | **299.6** | 145.2 | *226.5* | 145.8 |
| user-liked | **523.9** | **407.4** | 150.2 | 140.5 |
| friends-feed | **3,276.7** | *211.3* | 138.9 | 136.2 |
| popular | **10,905.4** | **10,234.2** | **10,193.8** | **10,263.8** |
| trending-7d | **4,645.3** | **1,767.9** | **1,460.4** | **4,911.6** |

**Bold** marks cases where the database is the bottleneck. *Italic* marks values above 200 ms whose server execution time is below 1 ms. Those spikes come from the network (round-trip p95 = 167 ms), not from the query.

### Throughput at C = 4 (successful requests/s)

| Query | none | A | ABCD | ABCDEF |
|---|---:|---:|---:|---:|
| stats-one | 16.8 | 37.7 | 34.4 | 36.0 |
| stats-list | 15.4 | 37.1 | 32.7 | 36.9 |
| user-liked | 12.4 | 13.6 | 35.7 | 38.0 |
| friends-feed | 1.1 | 21.8 | 37.7 | 37.8 |
| popular | 0.0 | 0.0 | 0.0 | 0.0 |
| trending-7d | 1.1 | 3.8 | 4.7 | 1.8 |
| user-one (reference) | 37.2 | 38.0 | 37.9 | 31.8 |

With four clients and a round trip of about 105 ms, the maximum possible throughput is about 4 / 0.105 ≈ 38 req/s. Once indexed, every query except popular and trending-7d reaches that limit. At that point the network is the only remaining bottleneck.

## Step 3 — Minimum index set

- **A `likes(post_id)`** has the largest effect of any single index. It fixes stats-one, stats-list and most of friends-feed: execution falls from 1,548 ms to 43 ms, and C=4 throughput rises from 1.1 to 21.8 req/s.
- **C `likes(user_id, id DESC)`** is required for user-liked. Without it, user-liked stays above 350 ms p95 even when A is present.
- **B `posts(author_id, id DESC)`** and **D `friendships(user_id)`** remove the last two sequential scans from friends-feed (43 ms → 0.6 ms) and from author-posts (21–28 ms → 0.07 ms). These two queries were already close to the 200 ms limit with A alone, so B and D provide margin rather than a pass/fail change at this data size. As the posts and friendships tables grow, those sequential scans would grow too.
- **E `users(email)`** speeds the login lookup from 2.1 ms to 0.05 ms, but the p95 is set by the network round trip either way. Email uniqueness is still a data-integrity requirement, so the index belongs in production for that reason, but it is not needed for the latency target.
- **F `posts(created_at)`** did not help. The planner used it to find the 7-day window, but then hash-joined against a full sequential scan of likes. Execution rose from 223 ms to 333 ms and the C=4 p95 rose from 1,460 ms to 4,912 ms. F was dropped.

**Recommended set: A, B, C, D (4 indexes).** With these four, 8 of the 10 queries execute in under 3 ms on the server, and 7 of them in under 1 ms. At C = 1 and C = 4 their p95 is set by the network: the median is around 130–180 ms, and occasional spikes above 200 ms do not track any change in the plan. Of the 6 queries that were slow at baseline, 4 are fixed.

## Queries that indexes cannot fix

**popular** counts likes for all 200,000 posts and sorts them by count. It must read all 1,000,000 likes in every configuration, so no index removes that work. Execution varied between 6 and 18 s across runs, which reflects the shared Supabase compute. Nearly every request hit the 10 s timeout. **trending-7d** has the same problem on a smaller scale: about 16,000 recent posts and their likes, 200–460 ms of execution, and severe queueing at C = 4.

Both need a schema change rather than an index:

- a `like_count` column on `posts`, kept up to date by a trigger or by the application, plus an index on `(like_count DESC)`, so that popular becomes a 20-row index scan; or
- a materialized view of like counts (for example, per post per day), refreshed every few minutes, which trending-7d can read.

These options trade write cost and freshness for read latency. They are left for later work.

## Comparison with no indexes

| Metric | No secondary indexes | A + B + C + D |
|---|---:|---:|
| Queries with server execution > 100 ms | 6 / 10 | 2 / 10 |
| Queries with p95 > 200 ms at C = 1 (database-bound) | 6 / 10 | 2 / 10 |
| Sum of median execution, 8 indexable queries | 2,232 ms | 3.9 ms |
| friends-feed throughput at C = 4 | 1.1 req/s | 37.7 req/s |
| Time to build the four indexes + ANALYZE (1.42 M rows) | — | 6.3 s |

## Limitations

- Each stage measured only 10 s, about 50–95 requests at C = 1, so a single network spike can move the p95 by 50–200 ms. The italic values in the tables are an example. Server-side EXPLAIN times are the more reliable signal of index benefit, and the client p95 shows what a user would actually experience.
- The load test calls the database directly, without the FastAPI layer. In the full application, the M4 authentication reads add at least two more round trips (about 190 ms), and the three-connection pool limits throughput to about 6.7 req/s. The index gains measured here therefore show up in the application as removed database time, not as the absolute latencies shown above.
- Supabase compute is shared. The large spread in popular's execution time (6–18 s) shows that the same plan can run at very different speeds.
- The workload distributions are stated assumptions, as in M4. Only cumulative configurations were measured; each index was not tested in every combination.
