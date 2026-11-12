#!/usr/bin/env python3
"""Build M6 report figures (PDF) and LaTeX tables from m6/results/*.json."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
RES = ROOT / "m6/results"
FIG = ROOT / "m6/report/figures"
TAB = ROOT / "m6/report/tables"
FIG.mkdir(parents=True, exist_ok=True)
TAB.mkdir(parents=True, exist_ok=True)
C1, C2 = "#2a6fdb", "#e0731f"  # 1 shard (before), 2 shards (after)
plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
                     "figure.dpi": 150, "savefig.bbox": "tight"})


def load(label):
    p = RES / f"{label}.json"
    return json.loads(p.read_text()) if p.exists() else None


def staircase(n):
    rows = []
    for c in [1, 4, 8, 16, 32, 48, 64, 96, 128, 160, 192, 256]:
        r = load(f"S{n}-post-c{c}")
        if r:
            rows.append(r)
    return rows


def fmt(x, d=1):
    if x is None:
        return "--"
    if isinstance(x, float) and x != x:
        return "--"
    return f"{x:,.{d}f}"


def tex_escape(s: str) -> str:
    return (s.replace("\\", r"\textbackslash{}").replace("_", r"\_").replace("%", r"\%")
            .replace("$", r"\$").replace("&", r"\&").replace("#", r"\#").replace("{", r"\{")
            .replace("}", r"\}"))


def fig_throughput(s1, s2):
    fig, ax = plt.subplots(1, 2, figsize=(7.2, 2.8))
    for rows, col, lab in [(s1, C1, "Before: 1 database"), (s2, C2, "After: 2 shards")]:
        if not rows:
            continue
        cs = [r["concurrency"] for r in rows]
        ax[0].plot(cs, [r["rps_success"] for r in rows], "o-", color=col, label=lab)
        ax[1].plot(cs, [r["p50_ms"] for r in rows], "o--", color=col, label=f"{lab} p50")
        ax[1].plot(cs, [r["p99_ms"] for r in rows], "s-", color=col, label=f"{lab} p99")
    for n, col in [(1, C1), (2, C2)]:
        reps = [r for r in [load(f"S{n}-post-c{c}-repeat") for c in (64, 128, 256)] if r]
        ax[0].plot([r["concurrency"] for r in reps], [r["rps_success"] for r in reps], "o", mfc="white",
                   color=col, label=f"repeat ({'before' if n == 1 else 'after'})")
    for a in ax:
        a.set_xscale("log", base=2)
        a.set_xticks([1, 4, 8, 16, 32, 64, 128, 256])
        a.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
        a.set_xlabel("closed-loop clients (distinct users)")
        a.grid(alpha=0.3)
    ax[0].set_ylabel("successful posts / s")
    ax[0].set_title("Publish throughput")
    ax[1].set_ylabel("latency (ms)")
    ax[1].set_yscale("log")
    ax[1].set_title("Publish latency")
    ax[0].legend(frameon=False)
    ax[1].legend(frameon=False, fontsize=7)
    fig.savefig(FIG / "post_throughput_latency.pdf")
    plt.close(fig)


def fig_db(s1, s2):
    fig, ax = plt.subplots(1, 2, figsize=(7.2, 2.8))
    for rows, n, col in [(s1, 1, C1), (s2, 2, C2)]:
        if not rows:
            continue
        cs = [r["concurrency"] for r in rows]
        shards = list(rows[0]["db"])
        for i, s in enumerate(shards):
            ls = "-" if i == 0 else ":"
            lab = f"{'before' if n == 1 else 'after'} {s}"
            ax[0].plot(cs, [r["db"][s]["rows_inserted_per_s"] / 1000 for r in rows], "o" + ls, color=col, label=lab)
            ax[1].plot(cs, [r["db_active_backends_mean"][s] for r in rows], "o" + ls, color=col, label=lab)
    for a in ax:
        a.set_xscale("log", base=2)
        a.set_xticks([1, 4, 8, 16, 32, 64, 128, 256])
        a.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
        a.set_xlabel("closed-loop clients")
        a.grid(alpha=0.3)
        a.legend(frameon=False, fontsize=7)
    ax[0].set_ylabel("rows inserted / s (thousands)")
    ax[0].set_title("Write volume applied by each database")
    ax[1].set_ylabel("active backends (1 Hz mean)")
    ax[1].set_title("Busy connections per database")
    fig.savefig(FIG / "db_per_shard.pdf")
    plt.close(fig)


def fig_waits(rows, name, title):
    if not rows:
        return
    shard = list(rows[0]["db"])[0]
    cats = defaultdict(lambda: [0.0] * len(rows))
    for i, r in enumerate(rows):
        for k, v in r["db_top_waits"][shard].items():
            key = k if k in ("CPU:running", "LWLock:WALWrite", "IO:WalSync", "Client:ClientRead") else (
                "Lock/LWLock (other)" if k.startswith(("Lock", "LWLock")) else
                "IO (other)" if k.startswith("IO") else "other")
            cats[key][i] += v
    order = ["CPU:running", "LWLock:WALWrite", "IO:WalSync", "IO (other)", "Lock/LWLock (other)",
             "Client:ClientRead", "other"]
    colors = ["#4c78a8", "#e45756", "#f58518", "#ffbf79", "#b279a2", "#bab0ac", "#dddddd"]
    fig, ax = plt.subplots(figsize=(4.6, 2.7))
    x = range(len(rows))
    bottom = [0.0] * len(rows)
    for k, col in zip(order, colors):
        if k in cats:
            ax.bar(x, cats[k], bottom=bottom, color=col, label=k, width=0.7)
            bottom = [b + v for b, v in zip(bottom, cats[k])]
    ax.set_xticks(list(x), [str(r["concurrency"]) for r in rows])
    ax.set_xlabel("closed-loop clients")
    ax.set_ylabel("mean active backends")
    ax.set_title(title)
    ax.legend(frameon=False, fontsize=6.5, loc="upper left")
    fig.savefig(FIG / name)
    plt.close(fig)


def fig_workloads(pairs):
    labs = [p[0] for p in pairs if p[1] and p[2]]
    if not labs:
        return
    v1 = [p[1]["rps_success"] for p in pairs if p[1] and p[2]]
    v2 = [p[2]["rps_success"] for p in pairs if p[1] and p[2]]
    fig, ax = plt.subplots(figsize=(4.6, 2.6))
    x = range(len(labs))
    ax.bar([i - 0.18 for i in x], v1, 0.36, color=C1, label="Before: 1 database")
    ax.bar([i + 0.18 for i in x], v2, 0.36, color=C2, label="After: 2 shards")
    for i, (a, b) in enumerate(zip(v1, v2)):
        ax.text(i + 0.18, b, f"{b / a:.2f}x", ha="center", va="bottom", fontsize=7)
    ax.set_xticks(list(x), labs)
    ax.set_ylabel("successful requests / s")
    ax.set_title("Workloads at 128 clients")
    ax.legend(frameon=False, fontsize=7)
    fig.savefig(FIG / "workloads_c64.pdf")
    plt.close(fig)


def host_intervals():
    """CPU per Supabase metrics refresh (~60 s) from dbhost-timeline.jsonl."""
    p = RES / "dbhost-timeline.jsonl"
    if not p.exists():
        return {}
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    out = defaultdict(list)
    for s in ("shard_0", "shard_1"):
        r = sorted((x for x in rows if x["shard"] == s), key=lambda x: x["t"])
        for a, b in zip(r, r[1:]):
            tot = sum(b["cpu"].values()) - sum(a["cpu"].values())
            if tot <= 0 or b["t"] - a["t"] > 150:
                continue
            idle = b["cpu"]["idle"] - a["cpu"]["idle"]
            iow = b["cpu"].get("iowait", 0) - a["cpu"].get("iowait", 0)
            dt = b["t"] - a["t"]
            out[s].append({"t0": b["t"] - dt, "t1": b["t"], "busy": 100 * (tot - idle - iow) / tot,
                           "iowait": 100 * iow / tot,
                           "disk_mb_s": (b.get("node_disk_written_bytes_total", 0) - a.get("node_disk_written_bytes_total", 0)) / dt / 1e6})
    return out


def stage_windows():
    ev = [json.loads(l) for l in (RES / "timeline.jsonl").read_text().splitlines() if l.strip()]
    from datetime import datetime
    ts = lambda u: datetime.fromisoformat(u).timestamp()  # noqa: E731
    starts = {e["label"]: ts(e["utc"]) for e in ev if e["event"] == "stage_start"}
    return [(lab, starts[lab], ts(e["utc"])) for e in ev if e["event"] == "stage_end"
            for lab in [e["label"]] if lab in starts and "fill" not in lab]


def fig_host_timeline():
    iv = host_intervals()
    if not iv:
        return
    wins = stage_windows()
    for series in ("S1", "S2"):
        w = [x for x in wins if x[0].startswith(series)]
        if not w:
            continue
        block = [w[0]]  # the contiguous main series (extra stages ran later)
        for x in w[1:]:
            if x[1] - block[-1][2] > 300:
                break
            block.append(x)
        w = block
        t0, t1 = w[0][1] - 75, w[-1][2] + 75
        shards = ["shard_0"] if series == "S1" else ["shard_0", "shard_1"]
        fig, axes = plt.subplots(len(shards), 1, figsize=(7.2, 1.9 * len(shards)), sharex=True, squeeze=False)
        for ax, s in zip(axes[:, 0], shards):
            for lab, a, b in w:
                ax.axvspan((a - t0) / 60, (b - t0) / 60, color="#f2e6d9", lw=0)
                if s == shards[0]:
                    ax.text(((a + b) / 2 - t0) / 60, 101, lab.split("-", 1)[1].replace("post-", ""),
                            rotation=90, fontsize=5.5, ha="center", va="bottom")
            pts = [x for x in iv[s] if x["t1"] >= t0 and x["t0"] <= t1]
            for x in pts:
                xs = [(x["t0"] - t0) / 60, (x["t1"] - t0) / 60]
                ax.fill_between(xs, 0, x["busy"], step="pre", color="#4c78a8", alpha=0.9, lw=0)
                ax.fill_between(xs, x["busy"], x["busy"] + x["iowait"], step="pre", color="#e45756", alpha=0.85, lw=0)
            ax.set_ylim(0, 100)
            ax.set_ylabel(f"{s} CPU %")
            ax.grid(alpha=0.3)
        axes[0, 0].plot([], [], color="#4c78a8", lw=6, label="busy (user+system)")
        axes[0, 0].plot([], [], color="#e45756", lw=6, label="iowait")
        axes[0, 0].legend(frameon=False, fontsize=7, loc="upper left", ncol=2, bbox_to_anchor=(0, 1.45))
        axes[-1, 0].set_xlabel("minutes since series start (shaded = measured stage; 2 vCPU Nano, ~60 s metric refresh)")
        fig.savefig(FIG / f"host_cpu_{series}.pdf")
        plt.close(fig)


def table_stages(rows, path):
    lines = []
    for r in rows:
        shards = list(r["db"])
        rows_ins = " / ".join(fmt(r["db"][s]["rows_inserted_per_s"] / 1000, 1) for s in shards)
        act = " / ".join(fmt(r["db_active_backends_mean"][s], 1) for s in shards)
        exe = " / ".join(fmt(r["db"][s]["db_exec_s_per_s"], 2) for s in shards)
        wal = " / ".join(fmt(r["db"][s]["wal_mb_per_s"], 1) for s in shards)
        lines.append(
            f"{r['workload']}{' (repeat)' if 'repeat' in r['label'] else ''} & {r['concurrency']} & {r['attempts']:,} & {fmt(r['rps_success'])} & "
            f"{fmt(r['p50_ms'])} & {fmt(r['p95_ms'])} & {fmt(r['p99_ms'])} & {fmt(r['max_ms'], 0)} & "
            f"{100 * r['error_rate']:.2f}\\% & {rows_ins} & {exe} & {act} & {wal} & "
            f"{fmt(r['api_cpu_mean_pct'], 0)} \\\\")
    path.write_text("\n".join(lines) + "\n")


def table_waits(rows, path):
    lines = []
    for r in rows:
        for s, w in r["db_top_waits"].items():
            top = sorted(w.items(), key=lambda kv: -kv[1])[:5]
            lines.append(f"{r['workload']} & {r['concurrency']} & {tex_escape(s)} & "
                         + ", ".join(f"{tex_escape(k)} {v:.2f}" for k, v in top) + r" \\")
    path.write_text("\n".join(lines) + "\n")


def table_step1(tag, path):
    p = RES / f"step1-{tag}.json"
    if not p.exists():
        return
    d = json.loads(p.read_text())
    lines = []
    for s, v in d.items():
        for q, calls, total, mean, rows, wal in v["top"][:6]:
            lines.append(f"{tex_escape(s)} & \\texttt{{{tex_escape(q[:62])}}} & {int(calls):,} & "
                         f"{int(float(total)):,} & {float(mean):.2f} & {float(wal) / 1e6:,.1f} \\\\")
    path.write_text("\n".join(lines) + "\n")


def table_little(rows, path):
    lines = []
    for r in rows:
        L = r["rps_success"] * r["mean_ms"] / 1000
        lines.append(f"{r['shards']} & {r['workload']} & {r['concurrency']} & {fmt(r['rps_success'])} & "
                     f"{fmt(r['mean_ms'])} & {L:.2f} \\\\")
    path.write_text("\n".join(lines) + "\n")


def host_table(rows, path):
    lines = []
    for r in rows:
        for s, h in (r.get("db_host") or {}).items():
            lines.append(f"{r['shards']} & {r['workload']} & {r['concurrency']} & {tex_escape(s)} & "
                         f"{fmt(h.get('cpu_busy_pct'))}\\% & {fmt(h.get('cpu_iowait_pct'))}\\% & "
                         f"{fmt(h.get('disk_write_mb_s'), 2)} & {fmt(h.get('disk_writes_per_s'), 0)} \\\\")
    path.write_text("\n".join(lines) + "\n" if lines else "")


def summary_csv(all_rows):
    keys = ["label", "shards", "workload", "concurrency", "started_utc", "duration_s", "attempts", "successes",
            "errors", "error_rate", "rps_success", "mean_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms",
            "api_cpu_mean_pct", "api_cpu_max_pct", "api_rss_max_mib"]
    with (RES / "summary.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(keys + ["shard", "rows_inserted_per_s", "commits_per_s", "db_exec_s_per_s",
                           "wal_mb_per_s", "active_backends_mean", "db_size_mb", "db_cpu_busy_pct"])
        for r in all_rows:
            for s, d in r["db"].items():
                w.writerow([r.get(k) for k in keys] + [s, d["rows_inserted_per_s"], d["commits_per_s"],
                           d["db_exec_s_per_s"], d["wal_mb_per_s"], r["db_active_backends_mean"][s],
                           d["db_size_mb"], (r.get("db_host") or {}).get(s, {}).get("cpu_busy_pct")])


def main():
    s1, s2 = staircase(1), staircase(2)
    fig_throughput(s1, s2)
    fig_host_timeline()
    fig_db(s1, s2)
    fig_waits(s1, "waits_before.pdf", "Before: where shard_0's backends wait")
    if s2:
        fig_waits(s2, "waits_after_shard0.pdf", "After: shard_0 backends")
    extra = {}
    for n in (1, 2):
        extra[n] = [load(f"S{n}-{x}") for x in ["post-c128-repeat", "like-c128", "feed-c128", "mix-c128", "post-c256-repeat"]]
    fig_workloads([("publish", load("S1-post-c128"), load("S2-post-c128")),
                   ("like", extra[1][1], extra[2][1]),
                   ("feed read", extra[1][2], extra[2][2]),
                   ("70/20/10 mix", extra[1][3], extra[2][3])])
    for n, rows in [(1, s1), (2, s2)]:
        allr = rows + [r for r in extra[n] if r]
        table_stages(allr, TAB / f"stages_S{n}.tex")
        table_waits(rows, TAB / f"waits_S{n}.tex")
        table_step1(f"S{n}", TAB / f"step1_S{n}.tex")
        host_table(allr, TAB / f"host_S{n}.tex")
    little = [r for r in [load(f"S{n}-post-c{c}") for n in (1, 2) for c in (16, 64, 256)] if r]
    table_little(little, TAB / "little.tex")
    every = sorted((json.loads(p.read_text()) for p in RES.glob("S?-*.json")
                    if not p.name.endswith("check.json")), key=lambda r: r["started_utc"])
    summary_csv(every)
    print("figures:", sorted(p.name for p in FIG.iterdir()))
    print("tables:", sorted(p.name for p in TAB.iterdir()))


if __name__ == "__main__":
    main()
