"""Campus Pulse M6: the social API on N hash-partitioned Supabase databases.

Writes (lecture Step 9): register = directory row, then user row on its shard;
publish = one publish_post() call on the author's shard, then fanout() on each
other shard that has followers (written second, retried); like = one row on the
post author's shard; follow = following row on the follower's shard, then the
followers row on the followee's shard (retried).
Reads (Step 10): login looks up the directory once; the token carries the shard,
so every per-user page is a one-shard query.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from pydantic import BaseModel, EmailStr, Field

from app_sharded.router import DIRECTORY, get_router

password_hash = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)
SESSION_TTL = timedelta(hours=24)
TAG_RE = re.compile(r"#(\w{1,40})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_router()
    yield
    get_router().close()


app = FastAPI(title="Campus Pulse M6 (sharded)", version="6.0.0", lifespan=lifespan)


# ---------- schemas ----------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class PostIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class LikeIn(BaseModel):
    author_id: uuid.UUID  # the app knows the author from the post it displayed


class FollowIn(BaseModel):
    user_id: uuid.UUID


class CurrentUser(BaseModel):
    id: uuid.UUID
    display_name: str
    shard: str


# ---------- helpers ----------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def retry(fn, attempts: int = 3):
    """Second writes (fan-out copy, followers row) are idempotent: retry them."""
    for i in range(attempts):
        try:
            return fn()
        except psycopg.OperationalError:
            if i == attempts - 1:
                raise
            time.sleep(0.2 * (i + 1))


def new_session(shard: str, user_id: uuid.UUID) -> str:
    raw = f"{shard}.{secrets.token_urlsafe(32)}"  # the token names its shard
    with get_router().conn(shard) as c:
        c.execute(
            "insert into m6.sessions (token_hash, user_id, expires_at) values (%s, %s, %s)",
            (hash_token(raw), user_id, datetime.now(timezone.utc) + SESSION_TTL),
        )
    return raw


_auth_cache: dict[str, tuple[float, CurrentUser]] = {}
_auth_lock = threading.Lock()
AUTH_CACHE_S = 30.0


def current_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> CurrentUser:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token",
                                 headers={"WWW-Authenticate": "Bearer"})
    if cred is None or cred.scheme.lower() != "bearer":
        raise unauthorized
    token = cred.credentials
    now = time.monotonic()
    with _auth_lock:
        hit = _auth_cache.get(token)
    if hit and hit[0] > now:
        return hit[1]
    shard = token.split(".", 1)[0]
    if shard not in get_router().pools:
        raise unauthorized
    with get_router().conn(shard) as c:
        row = c.execute(
            "select u.id, u.display_name from m6.sessions s join m6.users u on u.id = s.user_id "
            "where s.token_hash = %s and s.expires_at > now()",
            (hash_token(token),),
        ).fetchone()
    if row is None:
        raise unauthorized
    user = CurrentUser(id=row["id"], display_name=row["display_name"], shard=shard)
    with _auth_lock:
        _auth_cache[token] = (now + AUTH_CACHE_S, user)
    return user


# ---------- endpoints ----------
@app.get("/health")
def health():
    return {"status": "ok", "shards": sorted(get_router().pools)}


@app.post("/api/auth/register", status_code=201)
def register(p: RegisterIn):
    r = get_router()
    email = str(p.email).strip().lower()
    # A · reserve the email on the directory; the database makes the uuid
    with r.conn(DIRECTORY) as c:
        try:
            row = c.execute(
                "insert into m6.user_directory (email) values (%s) returning user_id", (email,)
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "An account with that email already exists")
    uid = row["user_id"]
    shard = r.shard_for(uid)
    pw = password_hash.hash(p.password)

    # B · insert the user on the shard returned by A; retry B with the SAME id
    def insert_user():
        with r.conn(shard) as c:
            c.execute(
                "insert into m6.users (id, email, display_name, password_hash) values (%s,%s,%s,%s) "
                "on conflict (id) do nothing",
                (uid, email, p.display_name.strip(), pw),
            )

    retry(insert_user)
    return {"access_token": new_session(shard, uid), "user_id": uid, "shard": shard}


@app.post("/api/auth/login")
def login(p: LoginIn):
    r = get_router()
    with r.conn(DIRECTORY) as c:
        d = c.execute(
            "select d.user_id, m.shard from m6.user_directory d "
            "join m6.bucket_map m on m.bucket = m6.shard_bucket(d.user_id) where d.email = %s",
            (str(p.email).strip().lower(),),
        ).fetchone()
    if d is None:
        raise HTTPException(401, "Incorrect email or password")
    with r.conn(d["shard"]) as c:
        u = c.execute("select password_hash from m6.users where id = %s", (d["user_id"],)).fetchone()
    if u is None or not password_hash.verify(p.password, u["password_hash"]):
        raise HTTPException(401, "Incorrect email or password")
    return {"access_token": new_session(d["shard"], d["user_id"]), "user_id": d["user_id"], "shard": d["shard"]}


@app.get("/api/me")
def me(user: CurrentUser = Depends(current_user)):
    return user


@app.post("/api/messages", status_code=201)
def publish(p: PostIn, user: CurrentUser = Depends(current_user)):
    r = get_router()
    body = p.body.strip()
    post_id = uuid.uuid4()  # made by the app, known before the INSERT
    tags = sorted({t.lower() for t in TAG_RE.findall(body)})[:10]
    with r.conn(user.shard) as c:
        row = c.execute(
            "select post_created_at, remote_followers from m6.publish_post(%s, %s, %s, %s)",
            (post_id, user.id, body, tags),
        ).fetchone()
    by_shard: dict[str, list[uuid.UUID]] = defaultdict(list)
    for f in row["remote_followers"] or []:
        by_shard[r.shard_for(f)].append(f)
    for shard, owners in by_shard.items():
        def copy(shard=shard, owners=owners):
            with r.conn(shard) as c:
                c.execute("select m6.fanout(%s, %s, %s, %s, %s)",
                          (post_id, user.id, body[:80], row["post_created_at"], owners))
        retry(copy)
    return {"id": post_id, "author_id": user.id, "created_at": row["post_created_at"], "tags": tags,
            "remote_copies": {s: len(o) for s, o in by_shard.items()}}


@app.patch("/api/messages/{post_id}")
def edit(post_id: uuid.UUID, p: PostIn, user: CurrentUser = Depends(current_user)):
    with get_router().conn(user.shard) as c:
        row = c.execute(
            "update m6.posts set body = %s, updated_at = now() where id = %s and author_id = %s "
            "returning id, updated_at", (p.body.strip(), post_id, user.id)).fetchone()
    if row is None:
        raise HTTPException(404, "Post not found or not yours")
    return row


@app.post("/api/messages/{post_id}/like")
def like(post_id: uuid.UUID, p: LikeIn, user: CurrentUser = Depends(current_user)):
    shard = get_router().shard_for(p.author_id)  # likes live with the post
    with get_router().conn(shard) as c:
        try:
            c.execute(
                "insert into m6.likes (author_id, post_id, liker_id) values (%s,%s,%s) "
                "on conflict do nothing", (p.author_id, post_id, user.id))
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(404, "Post not found")
    return {"post_id": post_id, "liked": True}


@app.get("/api/posts/{author_id}/{post_id}")
def get_post(author_id: uuid.UUID, post_id: uuid.UUID, user: CurrentUser = Depends(current_user)):
    with get_router().conn(get_router().shard_for(author_id)) as c:
        row = c.execute(
            "select p.id, p.author_id, p.body, p.created_at, "
            "(select count(*) from m6.likes l where l.post_id = p.id) as likes, "
            "exists(select 1 from m6.likes l where l.post_id = p.id and l.liker_id = %s) as liked_by_me "
            "from m6.posts p where p.id = %s and p.author_id = %s", (user.id, post_id, author_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Post not found")
    return row


@app.get("/api/feed")
def feed(limit: int = Query(50, ge=1, le=100), user: CurrentUser = Depends(current_user)):
    with get_router().conn(user.shard) as c:  # copies live on the reader's shard
        return c.execute(
            "select post_id, author_id, preview, created_at from m6.feed_items "
            "where owner_id = %s order by created_at desc limit %s", (user.id, limit)).fetchall()


@app.post("/api/follow", status_code=201)
def follow(p: FollowIn, user: CurrentUser = Depends(current_user)):
    if p.user_id == user.id:
        raise HTTPException(400, "You cannot follow yourself")
    r = get_router()
    followee_shard = r.shard_for(p.user_id)
    with r.conn(followee_shard) as c:
        if c.execute("select 1 from m6.users where id = %s", (p.user_id,)).fetchone() is None:
            raise HTTPException(404, "User not found")
    with r.conn(user.shard) as c:  # 1 · follower's shard first
        c.execute("insert into m6.following (follower_id, followee_id) values (%s,%s) on conflict do nothing",
                  (user.id, p.user_id))

    def second():  # 2 · followee's shard, retried until it lands
        with r.conn(followee_shard) as c:
            c.execute("insert into m6.followers (followee_id, follower_id) values (%s,%s) on conflict do nothing",
                      (p.user_id, user.id))

    retry(second)
    return {"follower": user.id, "followee": p.user_id}


@app.get("/api/following")
def following(user: CurrentUser = Depends(current_user)):
    with get_router().conn(user.shard) as c:
        return c.execute("select followee_id from m6.following where follower_id = %s", (user.id,)).fetchall()


@app.get("/api/users/{author_id}/posts")
def author_posts(author_id: uuid.UUID, user: CurrentUser = Depends(current_user)):
    with get_router().conn(get_router().shard_for(author_id)) as c:
        return c.execute("select id, body, created_at from m6.posts where author_id = %s "
                         "order by created_at desc limit 20", (author_id,)).fetchall()
