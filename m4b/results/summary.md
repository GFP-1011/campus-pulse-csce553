# M4b results

- Run (UTC): 2026-10-28T02:09:02+00:00 → 2026-10-28T02:25:07+00:00
- Schema `m4b`; 10.0 s measured + 3 s warm-up per stage
- Network round trip `SELECT 1`: median 93.88 ms, p95 167.44 ms

Index codes: **A** = `likes_post_id`, **B** = `posts_author_id_id`, **C** = `likes_user_id_id`, **D** = `friendships_user_id`, **E** = `users_email`, **F** = `posts_created_at`

## EXPLAIN ANALYZE execution time (median ms) by index configuration

| Query | Tables | Shape | none | A | ABCD | ABCDEF |
|---|---:|---|---:|---:|---:|---:|
| user-one | 1 | single | 0.04 | 0.046 | 0.041 | 0.042 |
| user-by-email | 1 | single | 2.082 | 2.16 | 2.219 | 0.046 |
| post-list | 2 | list | 0.202 | 0.208 | 0.199 | 0.174 |
| author-posts | 2 | list | 20.83 | 28.309 | 0.065 | 0.069 |
| stats-one | 3 | single | 270.947 | 0.096 | 0.09 | 0.088 |
| stats-list | 3 | list | 190.838 | 0.456 | 0.46 | 0.424 |
| user-liked | 3 | list | 199.168 | 187.816 | 0.276 | 0.266 |
| friends-feed | 4 | list | 1547.711 | 43.424 | 0.58 | 0.566 |
| popular | 3 | list | 5968.861 | 18195.728 | 9070.075 | 10659.725 |
| trending-7d | 3 | list | 463.768 | 204.563 | 223.36 | 333.218 |

## Baseline (no secondary index) plan: where the time goes

| Query | Seq scans | Rows touched | Rows removed by filter | Exec ms |
|---|---|---:|---:|---:|
| user-one | - | 1 | 0 | 0.04 |
| user-by-email | users | 20,000 | 19,999 | 2.082 |
| post-list | - | 40 | 0 | 0.202 |
| author-posts | posts | 200,003 | 199,996 | 20.83 |
| stats-one | likes | 1,000,002 | 1,000,000 | 270.947 |
| stats-list | likes | 1,000,040 | 0 | 190.838 |
| user-liked | - | 1,000,040 | 999,956 | 199.168 |
| friends-feed | friendships;likes;posts | 20,400,020 | 20,199,870 | 1547.711 |
| popular | likes;posts;users | 1,220,000 | 0 | 5968.861 |
| trending-7d | likes;posts;users | 1,240,000 | 184,224 | 463.768 |

## Load test p95 latency (ms), C=1

| Query | none | A | ABCD | ABCDEF |
|---|---:|---:|---:|---:|
| user-one | 152.5 | 152.5 | 144.7 | 150.1 |
| user-by-email | 133.6 | 131.6 | 134.5 | 172.9 |
| post-list | 143.4 | 136.0 | 126.5 | 136.9 |
| author-posts | 193.2 | 185.6 | **349.3** | 142.6 |
| stats-one | **438.2** | 133.3 | **218.6** | 169.3 |
| stats-list | **257.0** | 184.6 | 182.7 | 182.2 |
| user-liked | **369.8** | **354.9** | 115.7 | 126.2 |
| friends-feed | **1719.3** | 145.5 | 174.9 | 180.1 |
| popular | **10113.1** | **10112.9** | **6003.2** | **6245.6** |
| trending-7d | **681.8** | **665.4** | **983.0** | **712.8** |

(**bold** = p95 above 200 ms)

### Throughput (successful req/s), C=1

| Query | none | A | ABCD | ABCDEF |
|---|---:|---:|---:|---:|
| user-one | 9.29 | 9.49 | 9.5 | 9.17 |
| user-by-email | 9.49 | 9.5 | 9.49 | 9.01 |
| post-list | 9.23 | 9.38 | 9.49 | 9.48 |
| author-posts | 7.53 | 7.55 | 7.59 | 9.08 |
| stats-one | 3.27 | 9.4 | 8.28 | 9.01 |
| stats-list | 4.74 | 7.44 | 7.68 | 7.19 |
| user-liked | 3.21 | 3.22 | 9.1 | 8.99 |
| friends-feed | 0.56 | 7.51 | 7.49 | 7.39 |
| popular | 0.05 | 0.0 | 0.09 | 0.08 |
| trending-7d | 2.28 | 1.77 | 1.48 | 2.23 |

## Load test p95 latency (ms), C=4

| Query | none | A | ABCD | ABCDEF |
|---|---:|---:|---:|---:|
| user-one | 146.8 | 149.5 | 142.3 | **395.1** |
| user-by-email | 136.3 | 132.7 | 131.5 | 158.5 |
| post-list | 141.0 | 130.9 | 143.8 | 144.7 |
| author-posts | 143.6 | **207.9** | **263.0** | 143.0 |
| stats-one | **299.9** | 143.6 | **213.6** | 150.8 |
| stats-list | **299.6** | 145.2 | **226.4** | 145.8 |
| user-liked | **523.9** | **407.4** | 150.2 | 140.5 |
| friends-feed | **3276.7** | **211.3** | 138.9 | 136.2 |
| popular | **10905.4** | **10234.2** | **10193.8** | **10263.8** |
| trending-7d | **4645.3** | **1768.0** | **1460.4** | **4911.6** |

(**bold** = p95 above 200 ms)

### Throughput (successful req/s), C=4

| Query | none | A | ABCD | ABCDEF |
|---|---:|---:|---:|---:|
| user-one | 37.24 | 37.96 | 37.93 | 31.84 |
| user-by-email | 38.01 | 37.89 | 37.95 | 34.73 |
| post-list | 37.92 | 37.94 | 37.9 | 36.88 |
| author-posts | 30.07 | 28.4 | 31.76 | 38.04 |
| stats-one | 16.77 | 37.7 | 34.44 | 36.03 |
| stats-list | 15.43 | 37.06 | 32.68 | 36.89 |
| user-liked | 12.41 | 13.59 | 35.73 | 37.96 |
| friends-feed | 1.14 | 21.82 | 37.69 | 37.8 |
| popular | 0.0 | 0.0 | 0.0 | 0.0 |
| trending-7d | 1.07 | 3.81 | 4.65 | 1.78 |

## Queries over 200 ms p95 per configuration

- C=1, `none`: 6 slow → stats-one, stats-list, user-liked, friends-feed, popular, trending-7d
- C=1, `A`: 3 slow → user-liked, popular, trending-7d
- C=1, `ABCD`: 4 slow → author-posts, stats-one, popular, trending-7d
- C=1, `ABCDEF`: 2 slow → popular, trending-7d
- C=4, `none`: 6 slow → stats-one, stats-list, user-liked, friends-feed, popular, trending-7d
- C=4, `A`: 5 slow → author-posts, user-liked, friends-feed, popular, trending-7d
- C=4, `ABCD`: 5 slow → author-posts, stats-one, stats-list, popular, trending-7d
- C=4, `ABCDEF`: 3 slow → user-one, popular, trending-7d
