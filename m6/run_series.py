#!/usr/bin/env python3
"""Run one M6 series: --shards 1 (before: one database) or --shards 2 (after).

Per series: build layout + seed + verify, start the API, then
  post staircase c=1,4,8,16,32,64,128,256 (stop rule: errors >10% or p99 >5 s),
  repeat c=128, then like, feed-read and 70/20/10 mix stages at c=128.
Each post stage: 5 s warmup, 30 s measured, then posts/feed truncated and 15 s
recovery, so every stage starts from the same table sizes (and stays under the
500 MB free-plan limit). The M5 pg_cron refresh on shard_0 is paused during the
series and re-enabled at the end.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m6"))
import shardctl  # noqa: E402

PY = str(ROOT / ".venv/bin/python")


def utc():
    return datetime.now(timezone.utc).isoformat()


def note(out: Path, event: str, **kw):
    with (out / "timeline.jsonl").open("a") as f:
        f.write(json.dumps({"utc": utc(), "event": event, **kw}, default=str) + "\n")
    print(utc()[11:19], event, kw if kw else "", flush=True)


def cron(active: bool):
    with shardctl.connect("shard_0") as c:
        r = c.execute("select jobid from cron.job where command ilike '%search.post_search%'").fetchall()
        for (jobid,) in r:
            c.execute("select cron.alter_job(%s, active := %s)", (jobid, active))
        return [j for (j,) in r]


def start_api(n: int, out: Path):
    log = open(out / f"api-{n}shard.log", "w")
    p = subprocess.Popen([str(ROOT / "m6/serve.sh"), str(n), "6", "8600"], stdout=log, stderr=log,
                         start_new_session=True)
    import urllib.request
    for _ in range(60):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8600/health", timeout=2) as r:
                if json.loads(r.read())["shards"] == [f"shard_{i}" for i in range(n)]:
                    return p
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    raise RuntimeError("API did not start")


def stop_api(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(20)
    except Exception:  # noqa: BLE001
        pass


def stage(n, out, label, workload, c, pid, warmup=5, duration=30):
    cmd = [PY, str(ROOT / "m6/load_m6.py"), "--shards", str(n), "--workload", workload,
           "--concurrency", str(c), "--warmup", str(warmup), "--duration", str(duration),
           "--label", label, "--out-dir", str(out), "--api-pids", str(pid)]
    note(out, "stage_start", label=label, workload=workload, concurrency=c)
    subprocess.run(cmd, check=True)
    row = json.loads((out / f"{label}.json").read_text())
    note(out, "stage_end", label=label, rps=row["rps_success"], p99=row["p99_ms"], err=row["error_rate"])
    return row


def step1(out: Path, n: int, tag: str):
    """Lecture Step 1 query (writes only) + top statements, per shard."""
    res = {}
    for s in shardctl.layout(n):
        with shardctl.connect(s) as c:
            w = c.execute(r"""select left(query, 60), calls, round(total_exec_time)
                from extensions.pg_stat_statements where query ~* '^\s*(insert|update|delete)'
                order by total_exec_time desc limit 10""").fetchall()
            t = c.execute(r"""select left(regexp_replace(query, '\s+', ' ', 'g'), 80), calls,
                round(total_exec_time), round(mean_exec_time::numeric, 3), rows, wal_bytes
                from extensions.pg_stat_statements where dbid = (select oid from pg_database where datname='postgres')
                order by total_exec_time desc limit 10""").fetchall()
        res[s] = {"writes": w, "top": t}
    (out / f"step1-{tag}.json").write_text(json.dumps(res, indent=1, default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=int, choices=[1, 2], required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "m6/results")
    ap.add_argument("--steps", default="1,4,8,16,32,64,128,256")
    ap.add_argument("--tail", action="store_true",
                    help="layout already seeded and staircase done: run only the remaining stages")
    a = ap.parse_args()
    n, out = a.shards, a.out
    out.mkdir(parents=True, exist_ok=True)
    tag = f"S{n}"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    note(out, "series_start", series=tag, commit=commit)
    jobs = cron(False)
    note(out, "m5_cron_paused", jobs=jobs)
    api = None
    try:
        if not a.tail:
            shardctl.setup(n)
            shardctl.seed(n, users=400, follows=40)
            chk = shardctl.check(n)
            (out / f"{tag}-check.json").write_text(json.dumps(chk, indent=1))
            for s in shardctl.layout(n):
                with shardctl.connect(s) as c:
                    c.execute("select extensions.pg_stat_statements_reset()")
        api = start_api(n, out)
        pid = api.pid
        note(out, "api_started", pid=pid, workers=6, pool_max_per_shard=32, children=len(psutil.Process(pid).children(recursive=True)))
        time.sleep(3)
        if not a.tail:
            for c in [int(x) for x in a.steps.split(",")]:
                r = stage(n, out, f"{tag}-post-c{c}", "post", c, pid)
                shardctl.reset_activity(n)
                time.sleep(15)
                if r["error_rate"] > 0.10 or (r["p99_ms"] or 0) > 5000:
                    note(out, "stop_rule", label=r["label"])
                    break
            step1(out, n, tag)
            stage(n, out, f"{tag}-post-c128-repeat", "post", 128, pid)
            shardctl.reset_activity(n)
            time.sleep(15)
        shardctl.like_posts(n, 2000)
        (out / "fill").mkdir(exist_ok=True)
        # fill every user's home feed (not measured) so the feed read returns 50 rows
        stage(n, out / "fill", f"{tag}-fill", "post", 32, pid, warmup=0, duration=10)
        for wl in ("like", "feed", "mix"):
            stage(n, out, f"{tag}-{wl}-c128", wl, 128, pid)
            time.sleep(10)
        shardctl.reset_activity(n)
    finally:
        if api:
            stop_api(api)
        note(out, "m5_cron_restored", jobs=cron(True))
        note(out, "series_end", series=tag)


if __name__ == "__main__":
    main()
