#!/usr/bin/env python3
"""M6 closed-loop load driver (adapted from the M2 driver m2/m2-tools/load_baseline.py).

Same method as M2 (closed loop, warmup discarded, fixed measurement window,
per-request CSV, p50/p95/p99/max, error rate) with two changes needed for
sharding: (1) every worker is a DIFFERENT user, so writes spread over owners
(one user always maps to one shard); (2) concurrency up to 256.
While the stage runs, a sampler records on every shard: active backends and
their wait events (pg_stat_activity, 1 s), and before/after deltas of
pg_stat_database, pg_stat_wal and pg_stat_statements; plus API CPU (psutil).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psutil
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m6"))
from shardctl import connect, layout  # noqa: E402

BODY = ("Campus Pulse M6 write test: study group at the library tonight, bring snacks "
        "#gameday #library #m6 post {n:08d}")


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def once(url, method, body, token, timeout):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "csce553-m6"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return (time.perf_counter() - t0) * 1000, r.status, ""
    except urllib.error.HTTPError as e:
        e.read()
        return (time.perf_counter() - t0) * 1000, e.code, "http"
    except Exception as e:  # noqa: BLE001
        return (time.perf_counter() - t0) * 1000, 0, type(e).__name__


class DbSampler(threading.Thread):
    """1 Hz pg_stat_activity sampler on every shard (non-idle client backends)."""

    def __init__(self, shards):
        super().__init__(daemon=True)
        self.shards, self.stop, self.rows = shards, threading.Event(), []
        self.conns = {s: connect(s) for s in shards}

    def run(self):
        q = ("select coalesce(wait_event_type,'CPU'), coalesce(wait_event,'running'), count(*) "
             "from pg_stat_activity where backend_type='client backend' and state='active' "
             "and pid <> pg_backend_pid() and datname = 'postgres' group by 1,2")
        while not self.stop.is_set():
            t = time.time()
            for s, c in self.conns.items():
                try:
                    for wt, we, n in c.execute(q).fetchall():
                        self.rows.append((t, s, wt, we, n))
                except Exception as e:  # noqa: BLE001
                    self.rows.append((t, s, "sampler_error", type(e).__name__, 0))
            self.stop.wait(max(0, 1 - (time.time() - t)))
        for c in self.conns.values():
            c.close()


def db_snapshot(shards):
    snap = {}
    for s in shards:
        with connect(s) as c:
            d = c.execute("select xact_commit, xact_rollback, tup_inserted, tup_updated, tup_deleted, "
                          "blks_read, blks_hit from pg_stat_database where datname='postgres'").fetchone()
            w = c.execute("select wal_records, wal_bytes, wal_buffers_full from pg_stat_wal").fetchone()
            st = c.execute("select queryid::text, left(regexp_replace(query, '\\s+', ' ', 'g'), 90), calls, "
                           "total_exec_time, rows, wal_bytes from extensions.pg_stat_statements "
                           "where query ilike '%m6.%'").fetchall()
            size = c.execute("select pg_database_size('postgres')").fetchone()[0]
        snap[s] = {"db": d, "wal": [w[0], float(w[1]), w[2]], "stmts": st, "size": size}
    return snap


def snap_delta(a, b, seconds):
    out = {}
    for s in a:
        da = dict(zip(["xact_commit", "xact_rollback", "tup_inserted", "tup_updated", "tup_deleted",
                       "blks_read", "blks_hit"], [y - x for x, y in zip(a[s]["db"], b[s]["db"])]))
        da["wal_records"] = b[s]["wal"][0] - a[s]["wal"][0]
        da["wal_bytes"] = b[s]["wal"][1] - a[s]["wal"][1]
        da["wal_buffers_full"] = b[s]["wal"][2] - a[s]["wal"][2]
        before = {r[0]: r for r in a[s]["stmts"]}
        stmts = []
        for r in b[s]["stmts"]:
            p = before.get(r[0], (None, None, 0, 0.0, 0, 0))
            calls = r[2] - p[2]
            if calls > 0:
                ex = r[3] - p[3]
                stmts.append({"query": r[1], "calls": calls, "total_exec_ms": round(ex, 1),
                              "mean_exec_ms": round(ex / calls, 3), "rows": r[4] - p[4],
                              "wal_bytes": float(r[5] - p[5])})
        stmts.sort(key=lambda x: -x["total_exec_ms"])
        da["db_exec_s_per_s"] = round(sum(x["total_exec_ms"] for x in stmts) / 1000 / seconds, 3)
        da["commits_per_s"] = round(da["xact_commit"] / seconds, 1)
        da["rows_inserted_per_s"] = round(da["tup_inserted"] / seconds, 1)
        da["wal_mb_per_s"] = round(da["wal_bytes"] / seconds / 1e6, 3)
        da["db_size_mb"] = round(b[s]["size"] / 1e6, 1)
        da["top_statements"] = stmts[:6]
        out[s] = da
    return out


REFS = {"shard_0": "SHARD_0_URL", "shard_1": "SHARD_1_URL"}


def _metrics_targets(shards):
    """Supabase Prometheus endpoint per shard, if SHARD_n_SECRET is in .env."""
    from shardctl import env
    e = env()
    out = {}
    for s in shards:
        key = e.get(f"{s.upper()}_SECRET")
        if key:
            ref = e[REFS[s]].split("postgres.", 1)[1].split(":", 1)[0]
            out[s] = (f"https://{ref}.supabase.co/customer/v1/privileged/metrics", key)
    return out


def scrape(url, key):
    import base64
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic " + base64.b64encode(f"service_role:{key}".encode()).decode()})
    with urllib.request.urlopen(req, timeout=10) as r:
        text = r.read().decode()
    cpu = {}
    vals = Counter()
    for line in text.splitlines():
        if line.startswith("node_cpu_seconds_total{"):
            labels, v = line.rsplit(" ", 1)
            mode = labels.split('mode="', 1)[1].split('"', 1)[0]
            cpu[mode] = cpu.get(mode, 0.0) + float(v)
        else:
            for m in ("node_disk_written_bytes_total", "node_disk_writes_completed_total",
                      "node_disk_io_time_seconds_total", "node_memory_MemAvailable_bytes",
                      "node_load1"):
                if line.startswith(m + "{") or line.startswith(m + " "):
                    vals[m] += float(line.rsplit(" ", 1)[1])
    ncpu = sum(1 for line in text.splitlines()
               if line.startswith("node_cpu_seconds_total{") and 'mode="idle"' in line)
    return cpu, vals, ncpu


def metrics_sampler(targets, stop, out, period=5.0):
    while not stop.is_set():
        t = time.time()
        for s, (url, key) in targets.items():
            try:
                cpu, vals, ncpu = scrape(url, key)
                out.append({"t": t, "shard": s, "cpu": cpu, "ncpu": ncpu, **vals})
            except Exception as e:  # noqa: BLE001
                out.append({"t": t, "shard": s, "error": type(e).__name__})
        stop.wait(max(0, period - (time.time() - t)))


def metrics_summary(rows, shards):
    res = {}
    for s in shards:
        r = [x for x in rows if x["shard"] == s and "cpu" in x]
        if len(r) < 2:
            continue
        a, b = r[0], r[-1]
        tot = sum(b["cpu"].values()) - sum(a["cpu"].values())
        busy = tot - (b["cpu"].get("idle", 0) - a["cpu"].get("idle", 0)) - (b["cpu"].get("iowait", 0) - a["cpu"].get("iowait", 0))
        iow = b["cpu"].get("iowait", 0) - a["cpu"].get("iowait", 0)
        dt = b["t"] - a["t"]
        res[s] = {"cpu_busy_pct": round(100 * busy / tot, 1) if tot else None,
                  "cpu_iowait_pct": round(100 * iow / tot, 1) if tot else None,
                  "ncpu": b["ncpu"],
                  "disk_write_mb_s": round((b.get("node_disk_written_bytes_total", 0) - a.get("node_disk_written_bytes_total", 0)) / dt / 1e6, 2),
                  "disk_writes_per_s": round((b.get("node_disk_writes_completed_total", 0) - a.get("node_disk_writes_completed_total", 0)) / dt, 1),
                  "disk_busy_pct": round(100 * (b.get("node_disk_io_time_seconds_total", 0) - a.get("node_disk_io_time_seconds_total", 0)) / dt, 1),
                  "load1_end": b.get("node_load1")}
    return res


def api_cpu_sampler(pids, stop, out):
    procs = []
    for p in pids:  # uvicorn master + its worker children
        m = psutil.Process(p)
        procs += [m] + m.children(recursive=True)
    for p in procs:
        p.cpu_percent(None)
    while not stop.is_set():
        stop.wait(2)
        out.append((time.time(), sum(p.cpu_percent(None) for p in procs if p.is_running()),
                    sum(p.memory_info().rss for p in procs if p.is_running()) / 2**20))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8600")
    ap.add_argument("--shards", type=int, choices=[1, 2], required=True)
    ap.add_argument("--workload", choices=["post", "like", "feed", "mix"], required=True)
    ap.add_argument("--concurrency", type=int, required=True)
    ap.add_argument("--warmup", type=float, default=10)
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--label", required=True)
    ap.add_argument("--out-dir", default="m6/results")
    ap.add_argument("--api-pids", default="", help="comma-separated uvicorn pids for CPU sampling")
    ap.add_argument("--mix", default="0.7,0.2,0.1", help="feed,like,post fractions for --workload mix")
    a = ap.parse_args()
    if not 1 <= a.concurrency <= 256:
        ap.error("concurrency must be 1..256")
    toks = json.loads((ROOT / f"m6/.private/tokens-{a.shards}shard.json").read_text())
    users = random.Random(a.label).sample(toks, a.concurrency)  # distinct users
    targets = []
    if a.workload in ("like", "mix"):
        targets = json.loads((ROOT / f"m6/.private/like-targets-{a.shards}shard.json").read_text())
    fr = [float(x) for x in a.mix.split(",")]
    counter = iter(range(10**9))
    lock = threading.Lock()

    def request(u, rng):
        kind = a.workload
        if kind == "mix":
            x = rng.random()
            kind = "feed" if x < fr[0] else ("like" if x < fr[0] + fr[1] else "post")
        if kind == "post":
            with lock:
                n = next(counter)
            return kind, once(a.base + "/api/messages", "POST", {"body": BODY.format(n=n)}, u["token"], a.timeout)
        if kind == "like":
            t = rng.choice(targets)
            return kind, once(f"{a.base}/api/messages/{t['post_id']}/like", "POST",
                              {"author_id": t["author_id"]}, u["token"], a.timeout)
        return kind, once(a.base + "/api/feed?limit=50", "GET", None, u["token"], a.timeout)

    def window(seconds, record):
        samples = []
        stop_at = time.perf_counter() + seconds

        def loop(i):
            rng = random.Random(f"{a.label}-{i}")
            u = users[i]
            while time.perf_counter() < stop_at:
                kind, (ms, code, err) = request(u, rng)
                if record:
                    with lock:
                        samples.append((time.time(), kind, u["shard"], ms, code, err))

        ts = [threading.Thread(target=loop, args=(i,)) for i in range(a.concurrency)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        return samples

    shards = list(layout(a.shards))
    print(f"{a.label}: warmup {a.warmup}s measure {a.duration}s c={a.concurrency} {a.workload} shards={shards}", flush=True)
    window(a.warmup, False)
    stop = threading.Event()
    cpu = []
    if a.api_pids:
        threading.Thread(target=api_cpu_sampler, args=([int(p) for p in a.api_pids.split(",")], stop, cpu),
                         daemon=True).start()
    sampler = DbSampler(shards)
    mrows: list = []
    mtargets = _metrics_targets(shards)
    if mtargets:
        threading.Thread(target=metrics_sampler, args=(mtargets, stop, mrows), daemon=True).start()
    s0 = db_snapshot(shards)
    sampler.start()
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()
    samples = window(a.duration, True)
    wall = time.perf_counter() - t0
    sampler.stop.set()
    stop.set()
    sampler.join()
    s1 = db_snapshot(shards)

    ok = sorted(s[3] for s in samples if 200 <= s[4] < 400)
    allms = sorted(s[3] for s in samples)
    errs = sum(1 for s in samples if not 200 <= s[4] < 400)
    by_kind = Counter((s[1], 200 <= s[4] < 400) for s in samples)
    by_user_shard = Counter(s[2] for s in samples if 200 <= s[4] < 400)
    n_samples = max(1, len({round(r[0]) for r in sampler.rows}))
    active = Counter()
    waits = Counter()
    for t, s, wt, we, n in sampler.rows:
        active[s] += n
        waits[(s, f"{wt}:{we}")] += n
    row = {
        "label": a.label, "shards": a.shards, "workload": a.workload, "concurrency": a.concurrency,
        "started_utc": started, "duration_s": round(wall, 3), "attempts": len(samples),
        "successes": len(ok), "errors": errs, "error_rate": round(errs / max(1, len(samples)), 4),
        "rps_success": round(len(ok) / wall, 3),
        "mean_ms": round(sum(allms) / max(1, len(allms)), 2),
        "p50_ms": round(pct(ok, 50), 2), "p95_ms": round(pct(ok, 95), 2),
        "p99_ms": round(pct(ok, 99), 2), "max_ms": round(ok[-1], 2) if ok else None,
        "by_kind": {f"{k}:{'ok' if g else 'err'}": v for (k, g), v in sorted(by_kind.items())},
        "ok_by_user_shard": dict(by_user_shard),
        "error_codes": dict(Counter(f"{s[4]}{s[5]}" for s in samples if not 200 <= s[4] < 400)),
        "api_cpu_mean_pct": round(sum(c[1] for c in cpu) / len(cpu), 1) if cpu else None,
        "api_cpu_max_pct": round(max(c[1] for c in cpu), 1) if cpu else None,
        "api_rss_max_mib": round(max(c[2] for c in cpu), 1) if cpu else None,
        "db_active_backends_mean": {s: round(active[s] / n_samples, 2) for s in shards},
        "db_top_waits": {s: {k[1]: round(v / n_samples, 2) for k, v in waits.most_common() if k[0] == s}
                         for s in shards},
        "db": snap_delta(s0, s1, wall),
        "db_host": metrics_summary(mrows, shards),
    }
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.label}.requests.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_unix", "kind", "user_shard", "latency_ms", "status", "error"])
        w.writerows((round(s[0], 3), s[1], s[2], round(s[3], 2), s[4], s[5]) for s in samples)
    with (out / f"{a.label}.dbwait.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_unix", "shard", "wait_type", "wait_event", "active"])
        w.writerows(sampler.rows)
    if cpu:
        with (out / f"{a.label}.apicpu.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_unix", "api_cpu_pct_sum", "api_rss_mib"])
            w.writerows(cpu)
    if mrows:
        (out / f"{a.label}.dbhost.jsonl").write_text("".join(json.dumps(m) + "\n" for m in mrows))
    (out / f"{a.label}.json").write_text(json.dumps(row, indent=1, default=str) + "\n")
    with (out / "stages.jsonl").open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")
    brief = {k: row[k] for k in ["rps_success", "p50_ms", "p99_ms", "error_rate", "api_cpu_mean_pct",
                                 "db_active_backends_mean"]}
    brief["db_exec_s/s"] = {s: row["db"][s]["db_exec_s_per_s"] for s in shards}
    brief["rows_ins/s"] = {s: row["db"][s]["rows_inserted_per_s"] for s in shards}
    brief["size_mb"] = {s: row["db"][s]["db_size_mb"] for s in shards}
    brief["db_cpu"] = {s: v.get("cpu_busy_pct") for s, v in row["db_host"].items()}
    print(json.dumps(brief), flush=True)


if __name__ == "__main__":
    main()
