#!/usr/bin/env python3
"""End-to-end check of Steps 9-10 through the HTTP API on the 2-shard layout.

Starts nothing itself: run `m6/serve.sh 2 2 8602` first. Registers fresh users until
both shards have one, then exercises register/duplicate/login/follow/publish/feed/
like/edit/logout-less token reuse, and inspects the rows on each shard directly.
Writes m6/results/smoke-S2.json.
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shardctl  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8602"
PW = "CampusPulseDemo!2026"
results = []


def call(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name, detail)


def main():
    run = uuid.uuid4().hex[:6]
    users = {}
    i = 0
    while len({u["shard"] for u in users.values()}) < 2 or len(users) < 2:
        email = f"smoke.{run}.{i}@campuspulse.example"
        st, b = call("POST", "/api/auth/register", {"email": email, "password": PW, "display_name": f"Smoke {i}"})
        assert st == 201, (st, b)
        users[email] = {"token": b["access_token"], "id": b["user_id"], "shard": b["shard"]}
        i += 1
    by_shard = {}
    for e, u in users.items():
        by_shard.setdefault(u["shard"], (e, u))
    (ea, a), (eb, b) = by_shard["shard_0"], by_shard["shard_1"]
    check("register lands users on both shards (Step 9 A+B)", True, f"{len(users)} registrations")

    st, _ = call("POST", "/api/auth/register", {"email": ea.upper(), "password": PW, "display_name": "dup"})
    check("duplicate email (any case) rejected by directory", st == 409, f"HTTP {st}")

    st, lg = call("POST", "/api/auth/login", {"email": eb, "password": PW})
    check("login: directory lookup returns the user's shard (Step 10)", st == 200 and lg["shard"] == "shard_1",
          f"HTTP {st}, shard={lg.get('shard')}")
    st, _ = call("POST", "/api/auth/login", {"email": eb, "password": "wrong-password"})
    check("login with wrong password rejected", st == 401, f"HTTP {st}")

    st, _ = call("POST", "/api/follow", {"user_id": b["id"]}, a["token"])
    check("A (shard_0) follows B (shard_1): two writes", st == 201, f"HTTP {st}")
    st, _ = call("POST", "/api/follow", {"user_id": b["id"]}, a["token"])
    check("repeating the follow is harmless (idempotent)", st == 201, f"HTTP {st}")
    with shardctl.connect("shard_0") as c:
        f0 = c.execute("select count(*) from m6.following where follower_id=%s and followee_id=%s", (a["id"], b["id"])).fetchone()[0]
    with shardctl.connect("shard_1") as c:
        f1 = c.execute("select count(*) from m6.followers where followee_id=%s and follower_id=%s", (b["id"], a["id"])).fetchone()[0]
    check("following row on follower's shard, followers row on followee's shard", f0 == 1 and f1 == 1, f"{f0}/{f1}")

    st, post = call("POST", "/api/messages", {"body": f"Smoke test post {run} #m6 #smoke"}, b["token"])
    check("B publishes; copy for A written second on shard_0", st == 201 and post["remote_copies"].get("shard_0") == 1,
          f"HTTP {st}, remote_copies={post.get('remote_copies')}, tags={post.get('tags')}")
    with shardctl.connect("shard_1") as c:
        p1 = c.execute("select count(*) from m6.posts where id=%s", (post["id"],)).fetchone()[0]
        t1 = c.execute("select count(*) from m6.post_tags where post_id=%s", (post["id"],)).fetchone()[0]
    with shardctl.connect("shard_0") as c:
        p0 = c.execute("select count(*) from m6.posts where id=%s", (post["id"],)).fetchone()[0]
    check("post + tags live only on the author's shard", p1 == 1 and t1 == 2 and p0 == 0, f"shard_1 post={p1} tags={t1}, shard_0 post={p0}")

    st, feed = call("GET", "/api/feed?limit=5", None, a["token"])
    check("A's home feed (one-shard read on shard_0) shows B's post", st == 200 and any(x["post_id"] == post["id"] for x in feed),
          f"HTTP {st}, {len(feed)} items")

    st, _ = call("POST", f"/api/messages/{post['id']}/like", {"author_id": b["id"]}, a["token"])
    st2, _ = call("POST", f"/api/messages/{post['id']}/like", {"author_id": b["id"]}, a["token"])
    st3, view = call("GET", f"/api/posts/{b['id']}/{post['id']}", None, a["token"])
    check("A likes B's post twice: one like row on B's shard", st == 200 and st2 == 200 and view["likes"] == 1 and view["liked_by_me"],
          f"likes={view.get('likes')}, liked_by_me={view.get('liked_by_me')}")
    with shardctl.connect("shard_0") as c:
        l0 = c.execute("select count(*) from m6.likes where post_id=%s", (post["id"],)).fetchone()[0]
    check("no like row on the liker's shard", l0 == 0, f"shard_0 likes={l0}")

    st, _ = call("PATCH", f"/api/messages/{post['id']}", {"body": "edited #m6"}, a["token"])
    check("A cannot edit B's post", st == 404, f"HTTP {st}")
    st, ed = call("PATCH", f"/api/messages/{post['id']}", {"body": f"Smoke test post {run} edited #m6"}, b["token"])
    check("B edits own post (UPDATE on B's shard)", st == 200, f"HTTP {st}")

    st, _ = call("GET", "/api/feed", None, "shard_1.not-a-real-token")
    check("forged token rejected", st == 401, f"HTTP {st}")

    with shardctl.connect("shard_0") as c:
        try:
            c.execute("insert into m6.users (id, email, display_name, password_hash) values (%s,'x@example.com','x','x')",
                      (b["id"],))
            ok, det = False, "accepted"
        except Exception as e:  # noqa: BLE001
            ok, det = "users_belong_here" in str(e), str(e).splitlines()[0]
    check("inserting a shard_1 user on shard_0 is rejected", ok, det)

    out = Path(__file__).resolve().parent / "results/smoke-S2.json"
    out.write_text(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "base": BASE,
                               "results": results}, indent=1))
    print(f"{sum(r['ok'] for r in results)}/{len(results)} passed -> {out}")
    sys.exit(0 if all(r["ok"] for r in results) else 1)


if __name__ == "__main__":
    main()
