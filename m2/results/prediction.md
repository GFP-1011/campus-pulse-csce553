# Pre-measurement prediction

Recorded 2026-09-09T03:48:52Z, before any capacity run.
API baseline commit: `8f0d1bde5ac280ec1cc924bebdfdb7d5efb38ad6`.
Existing uncommitted UI/documentation edits are not performance changes.

## Assumptions

10% of registered users are active in the peak hour; each active user performs
30 actions/hour. Operations are 80% authenticated feed reads and 20% message
writes. Model a bounded feed of 100 messages, 30,000 response bytes/read,
300 response bytes/write, 200 logical stored bytes/write, one copy (M1).
Assume four equivalent peak hours/day, not 24 hours at peak. These are hypotheses,
not measured payloads or database sizes. Indexes, row overhead, WAL and backups
increase physical storage beyond this logical lower bound.

Peak RPS = registered × 0.10 × 30 / 3600.
Write RPS = peak RPS × 0.20.
Storage/day = write RPS × 4 × 3600 × 200 bytes × 1 copy.
Response bandwidth = peak RPS × (0.8 × 30000 + 0.2 × 300) bytes/s.
Bandwidth excludes request headers, TLS and transport overhead; decimal MB/GB.

| Registered | Active (10%) | Peak RPS | Write RPS | Logical storage/day | Response bandwidth |
|---:|---:|---:|---:|---:|---:|
| 10,000 | 1,000 | 8.333 | 1.667 | 4.8 MB | 200,500 B/s |
| 1,000,000 | 100,000 | 833.333 | 166.667 | 480 MB | 20,050,000 B/s |
| 100,000,000 | 10,000,000 | 83,333.333 | 16,666.667 | 48 GB | 2,005,000,000 B/s |

With assumed mean response time 0.2 s, Little's Law gives 1.667, 166.667 and
16,666.667 in-flight operations respectively. Replace the assumption with measured
mean latency after the runs; never substitute p99 into Little's Law.

## Falsifiable prediction

**The hosted API CPU will be the first limit on the authenticated read path,
observed as throughput flattening and p99 increasing while API CPU rises and
PostgreSQL connections remain bounded.**

Code evidence: `app/database.py` creates one reusable SQLAlchemy engine with
`pool_pre_ping=True`, not an engine per request; dependency sessions close after
each request. The installed PostgreSQL QueuePool configuration must be recorded
at run time. `app/api.py:list_posts` bounds the feed to 100 posts, joins authors
and likes, and constructs Python/Pydantic objects for every returned post.
Authentication also queries the database. Database round-trip/pool wait is a
competing explanation, especially for L; CPU saturation is not a measured fact.
Render's actual resource allocation must be verified from its dashboard.

The prediction is recorded in this file before measurement, but is not yet a
Git commit; commit this file before rerunning to provide stronger provenance.
