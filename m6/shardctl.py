#!/usr/bin/env python3
"""M6 shard control: build the layout (Steps 4-6, 8), seed users, verify (slide 41).

  python m6/shardctl.py setup --shards 1|2      # drop+create schema m6, checks, directory
  python m6/shardctl.py seed  --shards 1|2      # users, follow graph, sessions -> m6/.private
  python m6/shardctl.py check --shards 1|2      # counts, wrong-shard rows, bad insert
  python m6/shardctl.py reset-activity          # truncate posts/tags/likes/feed (between stages)
  python m6/shardctl.py like-posts --n 2000     # posts (no fan-out) used as like targets

Connection strings come from .env (SHARD_0_URL, SHARD_1_URL); never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
SQL = ROOT / "m6/sql"
PRIVATE = ROOT / "m6/.private"
sys.path.insert(0, str(ROOT))
from app_sharded.router import shard_bucket  # noqa: E402

DEMO_PASSWORD = "CampusPulseDemo!2026"  # fake course-only password (README)


def env() -> dict[str, str]:
    vals = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            vals.setdefault(k.strip(), v.strip())
    return vals


def connect(shard: str):
    return psycopg.connect(env()[f"{shard.upper()}_URL"], autocommit=True, connect_timeout=20)


def layout(n: int) -> dict[str, tuple[int, int]]:
    return {"shard_0": (0, 63)} if n == 1 else {"shard_0": (0, 31), "shard_1": (32, 63)}


def shard_of(uid: uuid.UUID, n: int) -> str:
    b = shard_bucket(uid)
    for s, (lo, hi) in layout(n).items():
        if lo <= b <= hi:
            return s
    raise AssertionError(b)


def setup(n: int) -> None:
    for shard in ["shard_0", "shard_1"]:
        if not env().get(f"{shard.upper()}_URL"):
            continue
        with connect(shard) as c:
            c.execute("drop schema if exists m6 cascade")
            if shard not in layout(n):
                print(f"{shard}: m6 dropped (not part of the {n}-shard layout)")
                continue
            c.execute((SQL / "shard_schema.sql").read_text())  # Step 4
            lo, hi = layout(n)[shard]
            c.execute((SQL / "shard_check.sql").read_text().format(lo=lo, hi=hi))  # Step 5
            if shard == "shard_0":
                c.execute((SQL / "directory.sql").read_text().format(n_shards=n))  # Step 6
            print(f"{shard}: schema m6 created, users_belong_here buckets {lo}..{hi}")
    # The app's Python bucket rule must equal m6.shard_bucket() on every shard.
    ids = [uuid.UUID(int=random.Random(7).getrandbits(128) ^ i, version=4) for i in range(2000)]
    for shard in layout(n):
        with connect(shard) as c:
            sql = c.execute("select m6.shard_bucket(u) from unnest(%s::uuid[]) u", (ids,)).fetchall()
        assert [r[0] for r in sql] == [shard_bucket(u) for u in ids], f"bucket mismatch on {shard}"
    print("bucket function: Python == SQL for 2000 ids on every shard")


def seed(n: int, users: int, follows: int, rnd_seed: int = 553) -> None:
    from pwdlib import PasswordHash

    rng = random.Random(rnd_seed)
    pw = PasswordHash.recommended().hash(DEMO_PASSWORD)
    ids = [uuid.UUID(int=rng.getrandbits(128), version=4) for _ in range(users)]
    people = [(u, f"m6.user{i:04d}@campuspulse.example", f"M6 User {i:04d}") for i, u in enumerate(ids)]
    edges: list[tuple[uuid.UUID, uuid.UUID]] = []  # each user follows exactly `follows` others
    for i, u in enumerate(ids):
        picks = [j for j in rng.sample(range(users), follows + 1) if j != i][:follows]
        edges.extend((u, ids[j]) for j in picks)

    expires = datetime.now(timezone.utc) + timedelta(days=7)
    tokens = []
    rows: dict[str, dict[str, list]] = {s: {"users": [], "following": [], "followers": [], "sessions": []}
                                       for s in layout(n)}
    import hashlib

    for u, email, name in people:
        s = shard_of(u, n)
        rows[s]["users"].append((u, email, name, pw))
        tok = f"{s}.{secrets.token_urlsafe(32)}"
        rows[s]["sessions"].append((hashlib.sha256(tok.encode()).hexdigest(), u, expires))
        tokens.append({"user_id": str(u), "shard": s, "token": tok, "email": email})
    for a, b in edges:
        rows[shard_of(a, n)]["following"].append((a, b))
        rows[shard_of(b, n)]["followers"].append((b, a))

    with connect("shard_0") as c:  # Step 9 A: directory
        with c.cursor().copy("copy m6.user_directory (user_id, email) from stdin") as cp:
            for u, email, _ in people:
                cp.write_row((u, email))
    for s, r in rows.items():  # Step 9 B: rows on the owner's shard
        with connect(s) as c:
            for table, cols in [("users", "id, email, display_name, password_hash"),
                                ("sessions", "token_hash, user_id, expires_at"),
                                ("following", "follower_id, followee_id"),
                                ("followers", "followee_id, follower_id")]:
                with c.cursor().copy(f"copy m6.{table} ({cols}) from stdin") as cp:
                    for row in r[table]:
                        cp.write_row(row)
            c.execute("analyze m6.users; analyze m6.followers; analyze m6.following; analyze m6.sessions")
        print(s, {k: len(v) for k, v in r.items()})
    PRIVATE.mkdir(exist_ok=True)
    (PRIVATE / f"tokens-{n}shard.json").write_text(json.dumps(tokens, indent=0))
    print(f"{len(tokens)} users, {len(edges)} follow edges; tokens -> m6/.private/tokens-{n}shard.json")


def check(n: int) -> dict:
    out: dict = {"layout": layout(n), "per_shard": {}}
    for s, (lo, hi) in layout(n).items():
        with connect(s) as c:
            users_here = c.execute("select count(*) from m6.users").fetchone()[0]
            wrong = c.execute(f"select count(*) from m6.users where m6.shard_bucket(id) not between {lo} and {hi}").fetchone()[0]
            out["per_shard"][s] = {"users": users_here, "wrong_shard": wrong}
    with connect("shard_0") as c:
        out["directory"] = dict(c.execute(
            "select m.shard, count(*) from m6.user_directory d join m6.bucket_map m "
            "on m.bucket = m6.shard_bucket(d.user_id) group by m.shard order by 1").fetchall())
        out["bucket_map"] = dict(c.execute("select shard, count(*) from m6.bucket_map group by shard order by 1").fetchall())
        if n == 2:
            try:
                c.execute("insert into m6.users (id, email, display_name, password_hash) values "
                          "('1d25ffe5-b534-402c-b01d-1b8cd2735f23','test@example.com','x','x')")
                out["bad_insert"] = "ACCEPTED (bug!)"
            except psycopg.errors.CheckViolation as e:
                out["bad_insert"] = "rejected: " + str(e).splitlines()[0]
    print(json.dumps(out, indent=1))
    return out


def reset_activity(n: int) -> None:
    for s in layout(n):
        with connect(s) as c:
            c.execute("truncate m6.feed_items, m6.likes, m6.post_tags, m6.posts")
            c.execute("analyze m6.posts; analyze m6.feed_items")


def like_posts(n: int, count: int) -> None:
    toks = json.loads((PRIVATE / f"tokens-{n}shard.json").read_text())
    rng = random.Random(99)
    per: dict[str, list] = {s: [] for s in layout(n)}
    targets = []
    for i in range(count):
        a = rng.choice(toks)
        pid = uuid.UUID(int=rng.getrandbits(128), version=4)
        per[a["shard"]].append((pid, a["user_id"], f"Like target {i} #m6 #gameday"))
        targets.append({"post_id": str(pid), "author_id": a["user_id"]})
    for s, rows in per.items():
        with connect(s) as c:
            with c.cursor().copy("copy m6.posts (id, author_id, body) from stdin") as cp:
                for r in rows:
                    cp.write_row(r)
    (PRIVATE / f"like-targets-{n}shard.json").write_text(json.dumps(targets))
    print({s: len(r) for s, r in per.items()}, "like targets")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["setup", "seed", "check", "reset-activity", "like-posts"])
    p.add_argument("--shards", type=int, choices=[1, 2], default=2)
    p.add_argument("--users", type=int, default=400)
    p.add_argument("--follows", type=int, default=40)
    p.add_argument("--n", type=int, default=2000)
    a = p.parse_args()
    if a.cmd == "setup":
        setup(a.shards)
    elif a.cmd == "seed":
        seed(a.shards, a.users, a.follows)
    elif a.cmd == "check":
        check(a.shards)
    elif a.cmd == "reset-activity":
        reset_activity(a.shards)
    else:
        like_posts(a.shards, a.n)


if __name__ == "__main__":
    main()
