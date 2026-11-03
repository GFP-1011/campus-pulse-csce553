#!/usr/bin/env python3
"""M5 closed-loop HTTP load test for GET /api/search (stdlib only).

Each stage repeats one search case with C concurrent clients:
warm-up (discarded) -> measurement -> short pause. Cases range from zero results to
tens of thousands of matches (see CASES in seed_search.py).

  python m5/loadtest_search.py --base http://127.0.0.1:8000            # local FastAPI -> Supabase
  python m5/loadtest_search.py --base https://campus-pulse-csce553.onrender.com --conc 1,4

Writes m5/results/load.csv and m5/results/summary.md.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_search import CASES  # noqa: E402

HERE = Path(__file__).resolve().parent


def pct(v, p):
    if not v:
        return float("nan")
    v = sorted(v)
    k = (len(v) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def http(method, url, body=None, headers=None, timeout=10.0):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            return (time.perf_counter() - t0) * 1000, r.status, payload
    except urllib.error.HTTPError as e:
        return (time.perf_counter() - t0) * 1000, e.code, e.read()
    except Exception as e:  # timeout, connection reset
        return (time.perf_counter() - t0) * 1000, 0, str(e).encode()


def get_token(base, email, password):
    _, st, p = http("POST", f"{base}/api/auth/login", {"email": email, "password": password}, timeout=60)
    if st != 200:
        _, st, p = http("POST", f"{base}/api/auth/register",
                        {"email": email, "password": password, "display_name": "M5 Load Tester"}, timeout=60)
    if st not in (200, 201):
        sys.exit(f"login/register failed: HTTP {st} {p[:200]!r}")
    return json.loads(p)["access_token"]


def run_stage(base, token, params, conc, warmup, duration, timeout):
    url = f"{base}/api/search?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {token}"}
    samples, lock = [], threading.Lock()
    t_start = time.perf_counter()
    t_meas, t_end = t_start + warmup, t_start + warmup + duration

    def worker():
        local = []
        while True:
            t0 = time.perf_counter()
            if t0 >= t_end:
                break
            ms, st, payload = http("GET", url, headers=headers, timeout=timeout)
            if t0 >= t_meas:
                info = {}
                if st == 200:
                    try:
                        d = json.loads(payload)
                        info = {"total": d.get("total"), "rows": len(d.get("results", [])), "db_ms": d.get("db_ms")}
                    except ValueError:
                        st = -1
                local.append((ms, st, info))
        with lock:
            samples.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(conc)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = max(time.perf_counter() - t_meas, 1e-9)
    lat = [s[0] for s in samples]
    ok = [s for s in samples if s[1] == 200]
    dbms = [s[2]["db_ms"] for s in ok if s[2].get("db_ms") is not None]
    first = ok[0][2] if ok else {}
    return {
        "requests": len(samples), "ok": len(ok),
        "rps": round(len(ok) / elapsed, 2),
        "mean_ms": round(statistics.mean(lat), 1) if lat else None,
        "p50_ms": round(pct(lat, 50), 1), "p95_ms": round(pct(lat, 95), 1), "p99_ms": round(pct(lat, 99), 1),
        "error_rate": round(1 - len(ok) / len(samples), 4) if samples else None,
        "db_ms_p50": round(pct(dbms, 50), 1) if dbms else None,
        "db_ms_p95": round(pct(dbms, 95), 1) if dbms else None,
        "matches": first.get("total"), "rows": first.get("rows"),
    }


def write_summary(rows, meta, path):
    concs = sorted({r["C"] for r in rows})
    cases = list(dict.fromkeys(r["case"] for r in rows))
    R = {(r["case"], r["C"]): r for r in rows}
    md = ["# M5 search load test", "",
          f"- Target: `{meta['base']}`  ·  run {meta['started_utc']} → {meta['finished_utc']} (UTC)",
          f"- Per stage: {meta['warmup_s']} s warm-up + {meta['duration_s']} s measured, timeout {meta['timeout_s']} s", "",
          "## Results", "",
          "| Case | Query | Matches | Rows | C | RPS | Mean ms | p50 ms | p95 ms | p99 ms | DB p50 ms | DB p95 ms | Errors |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        q = r["q"].replace("|", "\\|") + (f" · tag={r['tag']}" if r["tag"] else "") + \
            (f" · sort={r['sort']}" if r["sort"] != "relevance" else "") + (f" · page={r['page']}" if r["page"] != "1" else "")
        m = f"{r['matches']:,}" if isinstance(r["matches"], int) else "—"
        md.append(f"| {r['case']} | `{q}` | {m} | {r['rows'] if r['rows'] is not None else '—'} | {r['C']} | {r['rps']} | "
                  f"{r['mean_ms']} | {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | {r['db_ms_p50']} | {r['db_ms_p95']} | "
                  f"{(r['error_rate'] or 0)*100:.1f}% |")
    for c in concs:
        md += ["", f"## p95 (ms) by result size, C={c}", "", "| Case | Matches | p95 ms | DB p95 ms | RPS |", "|---|---:|---:|---:|---:|"]
        for case in sorted(cases, key=lambda k: (R.get((k, c), {}).get("matches") or 0)):
            r = R.get((case, c))
            if r:
                m = f"{r['matches']:,}" if isinstance(r["matches"], int) else "—"
                md.append(f"| {case} | {m} | {r['p95_ms']} | {r['db_ms_p95']} | {r['rps']} |")
    path.write_text("\n".join(md) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--email", default="m5-loadtest@example.com")
    ap.add_argument("--password", default="m5-loadtest-pass")
    ap.add_argument("--conc", default="1,4,8")
    ap.add_argument("--warmup", type=float, default=5)
    ap.add_argument("--duration", type=float, default=20)
    ap.add_argument("--pause", type=float, default=3)
    ap.add_argument("--timeout", type=float, default=10)
    ap.add_argument("--cases", help="comma list of case names (default: all)")
    ap.add_argument("--out", default=str(HERE / "results"))
    a = ap.parse_args()
    base = a.base.rstrip("/")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cases = [c for c in CASES if not a.cases or c[0] in a.cases.split(",")]
    concs = [int(c) for c in a.conc.split(",")]

    token = get_token(base, a.email, a.password)
    meta = {"base": base, "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "warmup_s": a.warmup, "duration_s": a.duration, "timeout_s": a.timeout}
    rows = []
    for name, params in cases:
        for c in concs:
            r = run_stage(base, token, params, c, a.warmup, a.duration, a.timeout)
            row = {"case": name, "q": params["q"], "tag": params.get("tag", ""), "sort": params.get("sort", "relevance"),
                   "page": params.get("page", "1"), "C": c, **r}
            rows.append(row)
            print(f"{name:<12} C={c:<2} matches={str(r['matches']):>7} rps={r['rps']:>6} "
                  f"p50={r['p50_ms']:>7} p95={r['p95_ms']:>7} db_p95={r['db_ms_p95']} err={r['error_rate']}", flush=True)
            time.sleep(a.pause)
    meta["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with (out / "load.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    write_summary(rows, meta, out / "summary.md")
    print(f"wrote {out/'load.csv'} and {out/'summary.md'}")


if __name__ == "__main__":
    main()
