#!/usr/bin/env python3
"""
M4b - Include Indexes (Campus Pulse, CSCE 553)

One-file benchmark that:
  1. builds an isolated schema (default `m4b`) on Supabase PostgreSQL with M4 "million"
     sized synthetic data and NO secondary indexes (primary keys only);
  2. runs 9 representative queries (1-4 tables, single row and list);
  3. for each index configuration (none -> add candidate indexes one at a time)
     records EXPLAIN (ANALYZE, BUFFERS) and a closed-loop load test (p50/p95/p99, RPS);
  4. writes CSV + JSON plans + a Markdown summary into ./results.

Usage
  pip install "psycopg[binary]"
  export DATABASE_URL='postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres'
  python m4b.py all                # setup + benchmark + report  (~30-40 min)
  python m4b.py setup              # only (re)create schema + data
  python m4b.py bench              # only benchmark (schema must exist)
  python m4b.py report             # rebuild summary.md from results/*.csv
  python m4b.py drop               # remove the m4b schema when finished

Options: --schema m4b --scale million|large|medium|small --duration 20 --warmup 3
         --conc 1,4 --configs <comma list of config names> --seed 553
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import random
import statistics
import sys
import threading
import time
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit('Missing driver: pip install "psycopg[binary]"')

# --------------------------------------------------------------------------- data
SCALES = {
    "small": dict(users=100, posts=1_000, likes=5_000, friends_per_user=10),
    "medium": dict(users=1_000, posts=10_000, likes=50_000, friends_per_user=10),
    "large": dict(users=10_000, posts=100_000, likes=500_000, friends_per_user=10),
    "million": dict(users=20_000, posts=200_000, likes=1_000_000, friends_per_user=10),
}
END_TS = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
SPAN_DAYS = 150

DDL = """
DROP SCHEMA IF EXISTS {s} CASCADE;
CREATE SCHEMA {s};
CREATE TABLE {s}.users (
  id          bigint PRIMARY KEY,
  username    text NOT NULL,
  email       text NOT NULL,
  created_at  timestamptz NOT NULL
);
CREATE TABLE {s}.posts (
  id          bigint PRIMARY KEY,
  author_id   bigint NOT NULL,
  body        text NOT NULL,
  created_at  timestamptz NOT NULL
);
CREATE TABLE {s}.likes (
  id          bigint PRIMARY KEY,
  user_id     bigint NOT NULL,
  post_id     bigint NOT NULL,
  created_at  timestamptz NOT NULL
);
CREATE TABLE {s}.friendships (
  id          bigint PRIMARY KEY,
  user_id     bigint NOT NULL,
  friend_id   bigint NOT NULL,
  created_at  timestamptz NOT NULL
);
"""
FKS = [
    "ALTER TABLE {s}.posts ADD FOREIGN KEY (author_id) REFERENCES {s}.users(id)",
    "ALTER TABLE {s}.likes ADD FOREIGN KEY (user_id) REFERENCES {s}.users(id)",
    "ALTER TABLE {s}.likes ADD FOREIGN KEY (post_id) REFERENCES {s}.posts(id)",
    "ALTER TABLE {s}.friendships ADD FOREIGN KEY (user_id) REFERENCES {s}.users(id)",
    "ALTER TABLE {s}.friendships ADD FOREIGN KEY (friend_id) REFERENCES {s}.users(id)",
]
# NOTE: PostgreSQL does not create indexes for foreign keys. Unique constraints such as
# users.email / likes(user_id, post_id) would create implicit indexes, so the baseline
# leaves them out on purpose; they are among the candidate indexes below.

WORDS = ("campus pulse event club study library exam lab coffee game music talk "
         "project team food game night lecture seminar deadline weekend trip").split()


def gen_data(cfg: dict, seed: int):
    rnd = random.Random(seed)
    U, P, L, F = cfg["users"], cfg["posts"], cfg["likes"], cfg["friends_per_user"]
    start = END_TS - dt.timedelta(days=SPAN_DAYS)

    users = []
    for i in range(1, U + 1):
        ts = start - dt.timedelta(days=rnd.uniform(0, 365))
        users.append((i, f"user{i:05d}", f"user{i:05d}@louisiana.edu", ts))

    # 80% of posts by 20% of users ("active authors")
    ids = list(range(1, U + 1))
    rnd.shuffle(ids)
    active, others = ids[: max(1, U // 5)], ids[U // 5:]
    # post ids increase with time (realistic: serial id + now())
    times = sorted(start + dt.timedelta(seconds=rnd.uniform(0, SPAN_DAYS * 86400)) for _ in range(P))
    posts = []
    for pid, ts in enumerate(times, start=1):
        author = rnd.choice(active) if rnd.random() < 0.8 else rnd.choice(others)
        body = " ".join(rnd.choice(WORDS) for _ in range(20))
        posts.append((pid, author, body, ts))

    # 80% of likes on 20% of posts; 30% of posts receive no likes
    pids = list(range(1, P + 1))
    rnd.shuffle(pids)
    popular = pids[: P // 5]
    warm = pids[P // 5: int(P * 0.7)]
    seen, likes = set(), []
    lid = 0
    while len(likes) < L:
        post = rnd.choice(popular) if rnd.random() < 0.8 else rnd.choice(warm)
        user = rnd.randint(1, U)
        if (user, post) in seen:
            continue
        seen.add((user, post))
        lid += 1
        pts = times[post - 1]
        likes.append((lid, user, post, pts + dt.timedelta(seconds=rnd.uniform(0, 3 * 86400))))
    # like ids in time order
    likes.sort(key=lambda r: r[3])
    likes = [(i, u, p, t) for i, (_, u, p, t) in enumerate(likes, start=1)]

    friends, fid = [], 0
    for u in range(1, U + 1):
        targets = set()
        while len(targets) < min(F, U - 1):
            v = rnd.randint(1, U)
            if v != u:
                targets.add(v)
        for v in targets:
            fid += 1
            friends.append((fid, u, v, start + dt.timedelta(seconds=rnd.uniform(0, SPAN_DAYS * 86400))))
    return users, posts, likes, friends


def copy_rows(cur, table: str, cols: str, rows):
    with cur.copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)


def cmd_setup(args):
    cfg = SCALES[args.scale]
    s = args.schema
    print(f"[setup] generating {args.scale} data: {cfg}")
    t0 = time.time()
    users, posts, likes, friends = gen_data(cfg, args.seed)
    print(f"[setup] generated in {time.time()-t0:.1f}s; loading into schema '{s}' ...")
    with connect(args) as conn:
        dbsize = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0] / 2**20
        print(f"[setup] current database size: {dbsize:.0f} MiB (Supabase free tier limit: 500 MiB)")
        if dbsize > 250 and not args.force:
            sys.exit("[setup] Not enough headroom for ~200 MiB of new data + indexes. Drop old M4 schemas first, e.g.\n"
                     "        DROP SCHEMA m4_small CASCADE; DROP SCHEMA m4_medium CASCADE; DROP SCHEMA m4_large CASCADE;\n"
                     "        (or rerun with --force, or use --scale large)")
        conn.execute("SET statement_timeout = 0")
        with conn.cursor() as cur:
            cur.execute(DDL.format(s=s))
            for name, cols, rows in (
                ("users", "id, username, email, created_at", users),
                ("posts", "id, author_id, body, created_at", posts),
                ("likes", "id, user_id, post_id, created_at", likes),
                ("friendships", "id, user_id, friend_id, created_at", friends),
            ):
                t1 = time.time()
                copy_rows(cur, f"{s}.{name}", cols, rows)
                print(f"[setup]   {name:<12} {len(rows):>9,} rows  {time.time()-t1:.1f}s")
        print("[setup] adding foreign keys (no indexes) ...")
        cur = conn.cursor()
        for fk in FKS:
            cur.execute(fk.format(s=s))
        conn.commit()
        conn.autocommit = True
        conn.execute(f"VACUUM ANALYZE {s}.users, {s}.posts, {s}.likes, {s}.friendships")
        print("[setup] distribution check:")
        for label, sql in DIST_SQL.items():
            print(f"[setup]   {label}: {conn.execute(sql.format(s=s)).fetchone()[0]}")
        size = conn.execute(
            "SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind='r'",
            (s,)).fetchone()[0]
        print(f"[setup]   total size (tables + PK indexes): {size}")
    print(f"[setup] done in {time.time()-t0:.1f}s")


DIST_SQL = {
    "posts by top-20% authors": """
        WITH c AS (SELECT author_id, count(*) n FROM {s}.posts GROUP BY 1),
             r AS (SELECT n, row_number() OVER (ORDER BY n DESC) rk FROM c)
        SELECT round(100.0*sum(n) FILTER (WHERE rk <= (SELECT count(*)/5 FROM {s}.users))/sum(n),2)||'%' FROM r""",
    "likes on top-20% posts": """
        WITH c AS (SELECT post_id, count(*) n FROM {s}.likes GROUP BY 1),
             r AS (SELECT n, row_number() OVER (ORDER BY n DESC) rk FROM c)
        SELECT round(100.0*sum(n) FILTER (WHERE rk <= (SELECT count(*)/5 FROM {s}.posts))/sum(n),2)||'%' FROM r""",
    "posts with zero likes": """
        SELECT count(*) FROM {s}.posts p WHERE NOT EXISTS (SELECT 1 FROM {s}.likes l WHERE l.post_id=p.id)""",
}

# ----------------------------------------------------------------------- queries
# Each query: name, tables, shape, SQL (schema placeholder {s}), param generator name.
QUERIES = [
    ("user-one", 1, "single",
     "SELECT id, username, email FROM {s}.users WHERE id = %(uid)s", "uid"),
    ("user-by-email", 1, "single",
     "SELECT id, username, email FROM {s}.users WHERE email = %(email)s", "email"),
    ("post-list", 2, "list",
     """SELECT p.id, p.body, p.created_at, u.username
        FROM {s}.posts p JOIN {s}.users u ON u.id = p.author_id
        WHERE p.id < %(before)s ORDER BY p.id DESC LIMIT 20""", "before"),
    ("author-posts", 2, "list",
     """SELECT p.id, p.body, p.created_at, u.username
        FROM {s}.posts p JOIN {s}.users u ON u.id = p.author_id
        WHERE p.author_id = %(uid)s ORDER BY p.id DESC LIMIT 20""", "uid"),
    ("stats-one", 3, "single",
     """SELECT p.id, p.body, u.username, count(l.id) AS likes
        FROM {s}.posts p JOIN {s}.users u ON u.id = p.author_id
        LEFT JOIN {s}.likes l ON l.post_id = p.id
        WHERE p.id = %(pid)s GROUP BY p.id, u.id""", "pid"),
    ("stats-list", 3, "list",
     """SELECT p.id, p.body, u.username, count(l.id) AS likes
        FROM (SELECT * FROM {s}.posts WHERE id < %(before)s ORDER BY id DESC LIMIT 20) p
        JOIN {s}.users u ON u.id = p.author_id
        LEFT JOIN {s}.likes l ON l.post_id = p.id
        GROUP BY p.id, p.body, u.username ORDER BY p.id DESC""", "before"),
    ("user-liked", 3, "list",
     """SELECT p.id, p.body, u.username AS author, l.created_at AS liked_at
        FROM {s}.likes l JOIN {s}.posts p ON p.id = l.post_id
        JOIN {s}.users u ON u.id = p.author_id
        WHERE l.user_id = %(uid)s ORDER BY l.id DESC LIMIT 20""", "uid"),
    ("friends-feed", 4, "list",
     """SELECT p.id, p.body, p.created_at, u.username,
               (SELECT count(*) FROM {s}.likes l WHERE l.post_id = p.id) AS likes
        FROM {s}.friendships f
        JOIN {s}.posts p ON p.author_id = f.friend_id
        JOIN {s}.users u ON u.id = p.author_id
        WHERE f.user_id = %(uid)s
        ORDER BY p.created_at DESC LIMIT 20""", "uid"),
    ("popular", 3, "list",
     """SELECT p.id, p.body, u.username, count(l.id) AS likes
        FROM {s}.posts p JOIN {s}.users u ON u.id = p.author_id
        LEFT JOIN {s}.likes l ON l.post_id = p.id
        GROUP BY p.id, p.body, u.username ORDER BY likes DESC, p.id DESC LIMIT 20""", "none"),
    ("trending-7d", 3, "list",
     """SELECT p.id, p.body, u.username, count(l.id) AS likes
        FROM {s}.posts p JOIN {s}.users u ON u.id = p.author_id
        LEFT JOIN {s}.likes l ON l.post_id = p.id
        WHERE p.created_at >= %(since)s
        GROUP BY p.id, p.body, u.username ORDER BY likes DESC, p.id DESC LIMIT 20""", "since"),
]

# Candidate indexes, added cumulatively in this order (most broadly useful first).
CANDIDATES = [
    ("A", "likes_post_id", "CREATE INDEX {n} ON {s}.likes (post_id)"),
    ("B", "posts_author_id_id", "CREATE INDEX {n} ON {s}.posts (author_id, id DESC)"),
    ("C", "likes_user_id_id", "CREATE INDEX {n} ON {s}.likes (user_id, id DESC)"),
    ("D", "friendships_user_id", "CREATE INDEX {n} ON {s}.friendships (user_id)"),
    ("E", "users_email", "CREATE UNIQUE INDEX {n} ON {s}.users (email)"),
    ("F", "posts_created_at", "CREATE INDEX {n} ON {s}.posts (created_at DESC)"),
]
DEFAULT_CONFIGS = ["none", "A", "AB", "ABC", "ABCD", "ABCDE", "ABCDEF"]


class Params:
    """Deterministic rotating parameters (same sequence for every configuration)."""

    def __init__(self, conn, schema, seed):
        s = schema
        self.U = conn.execute(f"SELECT max(id) FROM {s}.users").fetchone()[0]
        self.P = conn.execute(f"SELECT max(id) FROM {s}.posts").fetchone()[0]
        self.maxts = conn.execute(f"SELECT max(created_at) FROM {s}.posts").fetchone()[0]
        self.seed = seed

    def stream(self, kind, worker):
        rnd = random.Random(f"{self.seed}-{kind}-{worker}")
        while True:
            if kind == "uid":
                yield {"uid": rnd.randint(1, self.U)}
            elif kind == "email":
                yield {"email": f"user{rnd.randint(1, self.U):05d}@louisiana.edu"}
            elif kind == "pid":
                yield {"pid": rnd.randint(1, self.P)}
            elif kind == "before":  # page position somewhere in the newest 90%
                yield {"before": rnd.randint(int(self.P * 0.1), self.P + 1)}
            elif kind == "none":
                yield {}
            elif kind == "since":   # last 7 days, window end jittered within last 30 days
                end = self.maxts - dt.timedelta(days=rnd.uniform(0, 30))
                yield {"since": end - dt.timedelta(days=7)}


# --------------------------------------------------------------------- utilities
def connect(args, autocommit=False):
    url = args.dsn or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL (Supabase session pooler URI, port 5432) or pass --dsn")
    url = url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
    conn = psycopg.connect(url, autocommit=autocommit, prepare_threshold=None, connect_timeout=15)
    conn.execute("SET statement_timeout = '10s'")
    return conn


def pct(vals, p):
    if not vals:
        return float("nan")
    v = sorted(vals)
    k = (len(v) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def apply_config(conn, schema, config):
    wanted = set() if config == "none" else set(config)
    for code, name, ddl in CANDIDATES:
        conn.execute(f"DROP INDEX IF EXISTS {schema}.{name}")
    for code, name, ddl in CANDIDATES:
        if code in wanted:
            conn.execute(ddl.format(n=name, s=schema))
    conn.execute(f"ANALYZE {schema}.users, {schema}.posts, {schema}.likes, {schema}.friendships")
    return [name for code, name, _ in CANDIDATES if code in wanted]


def walk_plan(node, out):
    """Collect per-node stats from a JSON plan."""
    loops = node.get("Actual Loops", 1) or 1
    out.append({
        "type": node.get("Node Type"),
        "relation": node.get("Relation Name", ""),
        "index": node.get("Index Name", ""),
        "rows": node.get("Actual Rows", 0) * loops,
        "removed": (node.get("Rows Removed by Filter", 0) + node.get("Rows Removed by Join Filter", 0)) * loops,
        "total_ms": node.get("Actual Total Time", 0) * loops,
    })
    for ch in node.get("Plans", []) or []:
        walk_plan(ch, out)


def explain(conn, schema, q, params, plans_dir, config, reps=5, timeout_s=30):
    name, _, _, sql, kind = q
    sql = sql.format(s=schema)
    p = next(params.stream(kind, "explain"))  # same params each rep -> warm-cache median
    conn.execute(f"SET statement_timeout = '{int(timeout_s)}s'")
    times, plan_json, timed_out = [], None, False
    try:
        for i in range(reps):
            row = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, p).fetchone()[0]
            plan_json = row[0] if isinstance(row, list) else json.loads(row)[0]
            times.append(plan_json["Execution Time"])
            if plan_json["Execution Time"] > 1000 and i >= 0:   # slow query: 1-2 reps are enough
                if i >= 1 or plan_json["Execution Time"] > 5000:
                    break
    except psycopg.errors.QueryCanceled:
        timed_out = True
    if plan_json is None:  # fall back to the estimated plan so the scans are still visible
        row = conn.execute("EXPLAIN (FORMAT JSON) " + sql, p).fetchone()[0]
        plan_json = row[0] if isinstance(row, list) else json.loads(row)[0]
    conn.execute("SET statement_timeout = '10s'")
    nodes = []
    walk_plan(plan_json["Plan"], nodes)
    seq = [n for n in nodes if n["type"] == "Seq Scan"]
    idx = sorted({n["index"] for n in nodes if n["index"]})
    removed = sum(n["removed"] for n in nodes)
    scanned = sum(n["rows"] + n["removed"] for n in nodes if n["type"] in ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"))
    (plans_dir / f"{config}__{name}.json").write_text(json.dumps(plan_json, indent=1, default=str))
    if times and not timed_out and statistics.median(times) < 1000:
        text = conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, p).fetchall()
    else:
        text = conn.execute("EXPLAIN " + sql, p).fetchall()
        text = [(f"-- estimated plan (query took {'>' + str(timeout_s) + ' s, timed out' if timed_out else f'{statistics.median(times):.0f} ms'}; "
                 "actual timings in the .json file when available)",)] + list(text)
    (plans_dir / f"{config}__{name}.txt").write_text("\n".join(r[0] for r in text))
    return {
        "exec_ms_median": f">{timeout_s*1000}" if timed_out else round(statistics.median(times), 3),
        "planning_ms": round(plan_json.get("Planning Time", 0) or 0, 3),
        "seq_scans": ";".join(sorted({n["relation"] for n in seq})) or "-",
        "indexes_used": ";".join(idx) or "-",
        "rows_removed": int(removed),
        "rows_touched": int(scanned),
    }


def load_test(args, schema, q, params, conc, duration, warmup):
    name, _, _, sql, kind = q
    sql = sql.format(s=schema)
    lat, errs, lock = [], [0], threading.Lock()
    start_evt = threading.Barrier(conc + 1)
    t_warm_end = [0.0]
    t_end = [0.0]

    def worker(w):
        try:
            conn = connect(args, autocommit=True)
        except Exception as e:  # noqa
            print("   connect failed:", e)
            start_evt.wait()
            return
        gen = params.stream(kind, w)
        start_evt.wait()
        local, lerr = [], 0
        while True:
            t0 = time.perf_counter()
            if t0 >= t_end[0]:
                break
            try:
                conn.execute(sql, next(gen)).fetchall()
                ok = True
            except Exception:
                ok = False
                try:
                    conn.rollback()
                except Exception:
                    pass
            t1 = time.perf_counter()
            if t0 >= t_warm_end[0]:
                local.append((t1 - t0) * 1000)
                lerr += 0 if ok else 1
        conn.close()
        with lock:
            lat.extend(local)
            errs[0] += lerr

    threads = [threading.Thread(target=worker, args=(w,), daemon=True) for w in range(conc)]
    for t in threads:
        t.start()
    now = time.perf_counter()
    t_warm_end[0] = now + warmup
    t_end[0] = now + warmup + duration
    start_evt.wait()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t_warm_end[0]
    n = len(lat)
    return {
        "requests": n,
        "rps": round((n - errs[0]) / elapsed, 2) if elapsed > 0 else 0,
        "mean_ms": round(statistics.mean(lat), 2) if lat else None,
        "p50_ms": round(pct(lat, 50), 2),
        "p95_ms": round(pct(lat, 95), 2),
        "p99_ms": round(pct(lat, 99), 2),
        "error_rate": round(errs[0] / n, 4) if n else None,
    }


def rtt_probe(args, n=50):
    with connect(args, autocommit=True) as conn:
        for _ in range(5):
            conn.execute("SELECT 1").fetchall()
        v = []
        for _ in range(n):
            t0 = time.perf_counter()
            conn.execute("SELECT 1").fetchall()
            v.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(v), 2), round(pct(v, 95), 2)


# ---------------------------------------------------------------------- commands
def cmd_bench(args):
    out = Path(args.out)
    plans = out / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    configs = args.configs.split(",") if args.configs else DEFAULT_CONFIGS
    concs = [int(c) for c in args.conc.split(",")]
    qnames = args.queries.split(",") if args.queries else None
    queries = [q for q in QUERIES if not qnames or q[0] in qnames]

    rtt50, rtt95 = rtt_probe(args)
    meta = {"started_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "schema": args.schema, "configs": configs, "concurrency": concs,
            "duration_s": args.duration, "warmup_s": args.warmup,
            "rtt_select1_median_ms": rtt50, "rtt_select1_p95_ms": rtt95}
    print(f"[bench] network round trip (SELECT 1): median {rtt50} ms, p95 {rtt95} ms")

    done_e = {(r["config"], r["query"]) for r in read_csv(out / "explain.csv")} if (out / "explain.csv").exists() else set()
    done_l = {(r["config"], r["query"], r["C"]) for r in read_csv(out / "load.csv")} if (out / "load.csv").exists() else set()
    exp_f = (out / "explain.csv").open("a", newline="")
    load_f = (out / "load.csv").open("a", newline="")
    exp_w = load_w = None
    with connect(args, autocommit=True) as conn:
        params = Params(conn, args.schema, args.seed)
        conn.execute("SET statement_timeout = '0'")
        for config in configs:
            t0 = time.time()
            idx = apply_config(conn, args.schema, config)
            conn.execute("SET statement_timeout = '10s'")
            print(f"\n[bench] config {config:<7} indexes={idx or 'none'} (built in {time.time()-t0:.1f}s)")
            for q in queries:
                if (config, q[0]) in done_e and all((config, q[0], str(c)) in done_l for c in concs):
                    print(f"  {q[0]:<14} already measured, skipping")
                    continue
                e = explain(conn, args.schema, q, params, plans, config)
                row = {"config": config, "query": q[0], "tables": q[1], "shape": q[2], **e}
                if exp_w is None:
                    exp_w = csv.DictWriter(exp_f, fieldnames=list(row))
                    if exp_f.tell() == 0:
                        exp_w.writeheader()
                if (config, q[0]) not in done_e:
                    exp_w.writerow(row)
                    exp_f.flush()
                for c in concs:
                    if (config, q[0], str(c)) in done_l:
                        continue
                    r = load_test(args, args.schema, q, params, c, args.duration, args.warmup)
                    lrow = {"config": config, "query": q[0], "tables": q[1], "shape": q[2], "C": c,
                            "exec_ms_median": e["exec_ms_median"], **r}
                    if load_w is None:
                        load_w = csv.DictWriter(load_f, fieldnames=list(lrow))
                        if load_f.tell() == 0:
                            load_w.writeheader()
                    load_w.writerow(lrow)
                    load_f.flush()
                    print(f"  {q[0]:<14} C={c:<2} exec={str(e['exec_ms_median']):>9}ms  "
                          f"p95={r['p95_ms']:>9.2f}ms  rps={r['rps']:>7}  seq={e['seq_scans']}")
            conn.execute("SET statement_timeout = '0'")
    meta["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    exp_f.close()
    load_f.close()
    cmd_report(args)


def read_csv(p):
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def cmd_report(args):
    out = Path(args.out)
    exp = read_csv(out / "explain.csv")
    load = read_csv(out / "load.csv")
    meta = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
    configs = list(dict.fromkeys(r["config"] for r in load))
    queries = list(dict.fromkeys(r["query"] for r in load))
    concs = sorted({int(r["C"]) for r in load})
    L = {(r["config"], r["query"], int(r["C"])): r for r in load}
    E = {(r["config"], r["query"]): r for r in exp}
    names = {c: n for c, n, _ in CANDIDATES}
    md = ["# M4b results", ""]
    if meta:
        md += [f"- Run (UTC): {meta.get('started_utc')} → {meta.get('finished_utc')}",
               f"- Schema `{meta.get('schema')}`; {meta.get('duration_s')} s measured + {meta.get('warmup_s')} s warm-up per stage",
               f"- Network round trip `SELECT 1`: median {meta.get('rtt_select1_median_ms')} ms, p95 {meta.get('rtt_select1_p95_ms')} ms", ""]
    md += ["Index codes: " + ", ".join(f"**{c}** = `{n}`" for c, n in names.items()), ""]

    md += ["## EXPLAIN ANALYZE execution time (median ms) by index configuration", "",
           "| Query | Tables | Shape | " + " | ".join(configs) + " |",
           "|---|---:|---|" + "---:|" * len(configs)]
    for q in queries:
        any_r = next(r for r in exp if r["query"] == q)
        md.append(f"| {q} | {any_r['tables']} | {any_r['shape']} | " +
                  " | ".join(E.get((c, q), {}).get("exec_ms_median", "") for c in configs) + " |")

    md += ["", "## Baseline (no secondary index) plan: where the time goes", "",
           "| Query | Seq scans | Rows touched | Rows removed by filter | Exec ms |", "|---|---|---:|---:|---:|"]
    base = configs[0]
    for q in queries:
        r = E.get((base, q))
        if r:
            md.append(f"| {q} | {r['seq_scans']} | {int(r['rows_touched']):,} | {int(r['rows_removed']):,} | {r['exec_ms_median']} |")

    for c in concs:
        md += ["", f"## Load test p95 latency (ms), C={c}", "",
               "| Query | " + " | ".join(configs) + " |", "|---|" + "---:|" * len(configs)]
        for q in queries:
            cells = []
            for cf in configs:
                r = L.get((cf, q, c))
                if not r:
                    cells.append("")
                    continue
                v = float(r["p95_ms"])
                cells.append(f"**{v:.1f}**" if v > 200 else f"{v:.1f}")
            md.append(f"| {q} | " + " | ".join(cells) + " |")
        md += ["", "(**bold** = p95 above 200 ms)"]
        md += ["", f"### Throughput (successful req/s), C={c}", "",
               "| Query | " + " | ".join(configs) + " |", "|---|" + "---:|" * len(configs)]
        for q in queries:
            md.append(f"| {q} | " + " | ".join(L.get((cf, q, c), {}).get("rps", "") for cf in configs) + " |")

    md += ["", "## Queries over 200 ms p95 per configuration", ""]
    for c in concs:
        for cf in configs:
            slow = [q for q in queries if (cf, q, c) in L and float(L[(cf, q, c)]["p95_ms"]) > 200]
            md.append(f"- C={c}, `{cf}`: {len(slow)} slow → {', '.join(slow) or '—'}")
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print(f"\n[report] wrote {out/'summary.md'}")


def cmd_drop(args):
    with connect(args, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {args.schema} CASCADE")
    print(f"dropped schema {args.schema}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["all", "setup", "bench", "report", "drop"])
    ap.add_argument("--dsn")
    ap.add_argument("--schema", default="m4b")
    ap.add_argument("--scale", default="million", choices=list(SCALES))
    ap.add_argument("--seed", type=int, default=553)
    ap.add_argument("--duration", type=float, default=20)
    ap.add_argument("--warmup", type=float, default=3)
    ap.add_argument("--conc", default="1,4")
    ap.add_argument("--configs", help="e.g. none,A,AB,ABCDEF (letters = candidate indexes)")
    ap.add_argument("--queries", help="comma list of query names to run")
    ap.add_argument("--out", default="results")
    ap.add_argument("--force", action="store_true", help="skip the free-tier space check")
    args = ap.parse_args()
    if not args.schema.isidentifier():
        sys.exit("bad schema name")
    if args.command in ("all", "setup"):
        cmd_setup(args)
    if args.command in ("all", "bench"):
        cmd_bench(args)
    if args.command == "report":
        cmd_report(args)
    if args.command == "drop":
        cmd_drop(args)


if __name__ == "__main__":
    main()
