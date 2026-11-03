#!/usr/bin/env python3
"""M5 search data + query-plan tool for Campus Pulse.

Seeds realistic-looking posts into the app's own tables (public.users / public.posts /
public.likes) so that search terms have planned result counts: from nothing to many pages.

  export DATABASE_URL='postgresql://...:5432/postgres'      # Supabase session pooler
  python m5/seed_search.py seed            # 2,000 users, 100,000 posts, ~200,000 likes
  python m5/seed_search.py refresh         # refresh search.post_search (after seed)
  python m5/seed_search.py counts          # result counts of every load-test case
  python m5/seed_search.py explain         # EXPLAIN ANALYZE of every case -> m5/results/explain.csv
  python m5/seed_search.py clean           # delete everything this script inserted

Seeded accounts use the e-mail domain @seed.campuspulse.test and cannot log in.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit('pip install "psycopg[binary]"')

SEED_DOMAIN = "seed.campuspulse.test"
HERE = Path(__file__).resolve().parent
END = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
SPAN_DAYS = 120

# Base vocabulary (Zipf-weighted). Probe words below are deliberately NOT in this list.
BASE_WORDS = """
today class tonight anyone going really week need help time good new lab exam friday
morning coffee professor notes homework project team meeting room hall food lunch dinner
free pizza event club music game night weekend parking bus rain hot cold weather
office hours question answer quiz midterm paper essay deadline extension grade grades
semester spring fall advising register course section seat waitlist online lecture
slides recording zoom email canvas moodle portal print printer scanner laptop charger
wifi password lost found keys wallet jacket umbrella bike scooter walk run gym pool
basketball football soccer baseball softball tennis track stadium ticket tickets crowd
cheer band concert theater movie show art gallery museum photo camera picture video
party birthday celebration holiday break thanksgiving christmas vacation trip home
family parents roommate apartment dorm rent lease kitchen laundry cleaning noise
study group quiet loud coding python java database server cloud azure render deploy
internship career fair resume interview job offer salary research poster conference
volunteer service charity donation drive blood bank recycling garden coffee shop
breakfast tacos burgers sushi boba tea smoothie dessert cookies cake snacks vending
hungry tired stressed excited happy grateful proud nervous ready finally almost
""".split()
BASE_WORDS = list(dict.fromkeys(w for w in BASE_WORDS if w not in {"quiet"}))

FIRST = """Avery Jordan Taylor Morgan Riley Casey Jamie Quinn Peyton Cameron Hayden Logan
Emery Rowan Skyler Parker Reese Dakota Sage Blake Kendall Ellis Harper Finley""".split()
LAST = """Broussard Landry Guidry Hebert Fontenot Richard Arceneaux Boudreaux Comeaux Doucet
LeBlanc Mouton Savoie Trahan Breaux Prejean Romero Duhon Melancon Robichaux""".split()
RARE_LAST = "Thibodeaux"          # ~1% of users: author-name search
TAGS = ["campuslife", "gameday", "finals", "ragincajuns", "studygroup", "foodtruck",
        "crawfish", "gym", "internship", "weekend", "parking", "coffee"]

# Probe terms with planned number of posts (out of 100,000).
PROBES = {"origami": 3, "robotics": 40, "volleyball": 400, "library": 4000, "campus": 40000}
PHRASE = ("study", "group")       # inserted as an adjacent phrase in 1,500 posts
PHRASE_POSTS = 1500
QUIET_POSTS = 2000                # "quiet": used with "library -quiet"

# Load-test cases: (name, query params). Shared with loadtest_search.py.
CASES = [
    ("zero",        {"q": "zeppelin"}),
    ("tiny",        {"q": "origami"}),
    ("small",       {"q": "robotics"}),
    ("medium",      {"q": "volleyball"}),
    ("large",       {"q": "library"}),
    ("huge",        {"q": "campus"}),
    ("phrase",      {"q": '"study group"'}),
    ("and-2-words", {"q": "library robotics"}),
    ("exclude",     {"q": "library -quiet"}),
    ("or",          {"q": "origami or robotics"}),
    ("author-name", {"q": "thibodeaux"}),
    ("tag-filter",  {"q": "campus", "tag": "gameday"}),
    ("newest-sort", {"q": "campus", "sort": "newest"}),
    ("deep-page",   {"q": "volleyball", "page": "10"}),
]


def connect(autocommit=False):
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL")
    url = url.replace("postgresql+psycopg://", "postgresql://")
    conn = psycopg.connect(url, autocommit=autocommit, prepare_threshold=None, connect_timeout=15)
    conn.execute("SET statement_timeout = 0")
    return conn


def zipf_weights(n, s=1.05):
    return [1 / (i + 1) ** s for i in range(n)]


def make_posts(n_posts, n_users, rnd):
    w = zipf_weights(len(BASE_WORDS))
    bodies = []
    for _ in range(n_posts):
        words = rnd.choices(BASE_WORDS, weights=w, k=rnd.randint(10, 28))
        bodies.append(words)
    idx = list(range(n_posts))
    for word, count in PROBES.items():
        for i in rnd.sample(idx, count):
            bodies[i].insert(rnd.randint(0, len(bodies[i])), word)
    for i in rnd.sample(idx, PHRASE_POSTS):
        pos = rnd.randint(0, len(bodies[i]))
        bodies[i][pos:pos] = list(PHRASE)
    for i in rnd.sample(idx, QUIET_POSTS):
        bodies[i].insert(rnd.randint(0, len(bodies[i])), "quiet")
    tw = zipf_weights(len(TAGS), 0.9)
    out = []
    for words in bodies:
        k = rnd.choices([0, 1, 2], weights=[5, 4, 1])[0]
        tags = list(dict.fromkeys(rnd.choices(TAGS, weights=tw, k=k)))
        text = " ".join(words)
        text = text[0].upper() + text[1:] + rnd.choice([".", "!", "?", "..."])
        if tags:
            text += " " + " ".join("#" + t for t in tags)
        out.append(text)
    # 80% of posts by 20% of users, like M4
    users = list(range(n_users))
    rnd.shuffle(users)
    active = users[: n_users // 5]
    authors = [rnd.choice(active) if rnd.random() < 0.8 else rnd.choice(users) for _ in range(n_posts)]
    times = sorted(END - dt.timedelta(seconds=rnd.uniform(0, SPAN_DAYS * 86400)) for _ in range(n_posts))
    return list(zip(out, authors, times))


def cmd_seed(args):
    rnd = random.Random(args.seed)
    t0 = time.time()
    with connect() as conn:
        n_existing = conn.execute("select count(*) from users where email like %s", (f"%@{SEED_DOMAIN}",)).fetchone()[0]
        if n_existing:
            sys.exit(f"{n_existing} seeded users already exist; run 'clean' first.")
        users = []
        for i in range(args.users):
            last = RARE_LAST if i % 100 == 7 else rnd.choice(LAST)
            users.append((f"seed{i:05d}@{SEED_DOMAIN}", f"{rnd.choice(FIRST)} {last}",
                          "!seed-account-no-login", END - dt.timedelta(days=200)))
        with conn.cursor() as cur:
            with cur.copy("COPY users (email, display_name, password_hash, created_at) FROM STDIN") as cp:
                for u in users:
                    cp.write_row(u)
            uid = [r[0] for r in cur.execute("select id from users where email like %s order by email",
                                              (f"%@{SEED_DOMAIN}",))]
            print(f"users   {len(uid):>8,}  {time.time()-t0:.1f}s")
            posts = make_posts(args.posts, len(uid), rnd)
            with cur.copy("COPY posts (author_id, body, created_at, updated_at) FROM STDIN") as cp:
                for body, a, ts in posts:
                    cp.write_row((uid[a], body, ts, ts))
            pid = [r[0] for r in cur.execute(
                "select p.id from posts p join users u on u.id=p.author_id where u.email like %s order by p.id",
                (f"%@{SEED_DOMAIN}",))]
            print(f"posts   {len(pid):>8,}  {time.time()-t0:.1f}s")
            seen = set()
            popular = pid[: len(pid) // 5]
            with cur.copy("COPY likes (user_id, post_id, created_at) FROM STDIN") as cp:
                while len(seen) < args.likes:
                    p = rnd.choice(popular) if rnd.random() < 0.8 else rnd.choice(pid)
                    u = rnd.choice(uid)
                    if (u, p) in seen:
                        continue
                    seen.add((u, p))
                    cp.write_row((u, p, END))
            print(f"likes   {len(seen):>8,}  {time.time()-t0:.1f}s")
        conn.commit()
        conn.autocommit = True
        conn.execute("analyze users, posts, likes")
    print(f"seeded in {time.time()-t0:.1f}s - now run: python m5/seed_search.py refresh")


def cmd_refresh(args):
    with connect(autocommit=True) as conn:
        t0 = time.time()
        conn.execute("refresh materialized view search.post_search")
        conn.execute("analyze search.post_search")
        n = conn.execute("select count(*) from search.post_search").fetchone()[0]
        size = conn.execute("select pg_size_pretty(pg_total_relation_size('search.post_search'))").fetchone()[0]
        print(f"search.post_search: {n:,} rows, {size} incl. indexes, refreshed in {time.time()-t0:.1f}s")


def sql_args(params):
    return {
        "q": params["q"], "tag": params.get("tag"), "sort": params.get("sort", "relevance"),
        "off": (int(params.get("page", 1)) - 1) * 20,
    }


INLINE_SEARCH = """
  with query as (select websearch_to_tsquery('english', %(q)s) as tsq),
  matches as (
    select s.*, ts_rank(s.fts, query.tsq) as rank
    from search.post_search s, query
    where s.fts @@ query.tsq
      and (%(tag)s::text is null or s.tags @> array[lower(%(tag)s::text)])
  )
  select m.id, m.author_name, m.body, m.created_at, m.like_count, m.tags, m.rank,
         count(*) over () as total
  from matches m
  order by case when %(sort)s = 'newest' then null else m.rank end desc nulls last,
           m.created_at desc, m.id desc
  limit 20 offset %(off)s
"""


def cmd_counts(args):
    with connect() as conn:
        print(f"{'case':<12} {'query':<26} {'matches':>8} {'pages':>6}")
        for name, params in CASES:
            a = sql_args(params)
            n = conn.execute(
                "select count(*) from search.post_search where fts @@ websearch_to_tsquery('english', %(q)s)"
                " and (%(tag)s::text is null or tags @> array[%(tag)s::text])", a).fetchone()[0]
            label = params["q"] + (f" tag={params['tag']}" if "tag" in params else "")
            print(f"{name:<12} {label:<26} {n:>8,} {-(-n // 20):>6}")


def walk(node, out):
    out.append(node)
    for ch in node.get("Plans", []) or []:
        walk(ch, out)


def cmd_explain(args):
    out = HERE / "results"
    (out / "plans").mkdir(parents=True, exist_ok=True)
    rows = []
    with connect() as conn:
        for name, params in CASES:
            a = sql_args(params)
            times, plan = [], None
            for _ in range(5):
                plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + INLINE_SEARCH, a).fetchone()[0][0]
                times.append(plan["Execution Time"])
            nodes = []
            walk(plan["Plan"], nodes)
            idx = sorted({n.get("Index Name") for n in nodes if n.get("Index Name")})
            seq = sorted({n.get("Relation Name") for n in nodes if n["Node Type"] == "Seq Scan"})
            total = conn.execute("select count(*) from search.post_search where fts @@ websearch_to_tsquery('english', %(q)s)"
                                 " and (%(tag)s::text is null or tags @> array[%(tag)s::text])", a).fetchone()[0]
            text = conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + INLINE_SEARCH, a).fetchall()
            (out / "plans" / f"{name}.txt").write_text("\n".join(r[0] for r in text))
            rows.append({"case": name, "q": params["q"], "tag": params.get("tag", ""),
                         "sort": params.get("sort", "relevance"), "page": params.get("page", "1"),
                         "matches": total, "exec_ms_median": round(statistics.median(times), 3),
                         "indexes": ";".join(idx) or "-", "seq_scans": ";".join(seq) or "-"})
            print(f"{name:<12} matches={total:>7,}  exec={rows[-1]['exec_ms_median']:>9.2f} ms  idx={rows[-1]['indexes']}")
    with (out / "explain.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out/'explain.csv'} and {out/'plans'}/")


def cmd_clean(args):
    with connect() as conn:
        n = conn.execute("delete from users where email like %s", (f"%@{SEED_DOMAIN}",)).rowcount
        conn.commit()
        print(f"deleted {n} seeded users (their posts and likes cascade); now run refresh")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["seed", "refresh", "counts", "explain", "clean"])
    ap.add_argument("--users", type=int, default=2000)
    ap.add_argument("--posts", type=int, default=100_000)
    ap.add_argument("--likes", type=int, default=200_000)
    ap.add_argument("--seed", type=int, default=553)
    args = ap.parse_args()
    {"seed": cmd_seed, "refresh": cmd_refresh, "counts": cmd_counts,
     "explain": cmd_explain, "clean": cmd_clean}[args.command](args)


if __name__ == "__main__":
    main()
