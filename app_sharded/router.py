"""Shard routing for Campus Pulse M6 (lecture Part 2, Steps 7, 9, 10).

The app holds one connection pool per shard and picks one per request.
Routing: user id -> bucket = md5(id)[0:8] % 64 -> bucket_map -> shard name
-> SHARD_<n>_URL. The bucket function is identical to m6.shard_bucket().
"""
from __future__ import annotations

import hashlib
import os
import uuid
from contextlib import contextmanager
from functools import lru_cache

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

N_BUCKETS = 64  # never changes
DIRECTORY = "shard_0"  # directory + bucket_map live on shard_0 for class


def shard_bucket(user_id: uuid.UUID | str) -> int:
    """Same rule as SQL: ('x' || substr(md5(u::text),1,8))::bit(32)::bigint % 64."""
    return int(hashlib.md5(str(user_id).encode()).hexdigest()[:8], 16) % N_BUCKETS


def _pool_url(url: str) -> str:
    # Supabase transaction pooler (port 6543): many app clients share the
    # project's server connections; every statement here is autocommit.
    if os.environ.get("M6_POOL_MODE", "transaction") == "transaction":
        return url.replace(":5432/", ":6543/")
    return url


class Router:
    def __init__(self) -> None:
        self.active = int(os.environ.get("ACTIVE_SHARDS", "2"))
        names = [f"shard_{i}" for i in range(self.active)]
        max_size = int(os.environ.get("M6_POOL_MAX", "8"))
        self.pools: dict[str, ConnectionPool] = {}
        for name in names:
            url = os.environ[f"{name.upper()}_URL"]
            self.pools[name] = ConnectionPool(
                _pool_url(url),
                min_size=1,
                max_size=max_size,
                kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
                timeout=30,
                open=True,
            )
        with self.conn(DIRECTORY) as c:
            rows = c.execute("select bucket, shard from m6.bucket_map order by bucket").fetchall()
        self.bucket_map = {r["bucket"]: r["shard"] for r in rows}
        if len(self.bucket_map) != N_BUCKETS:
            raise RuntimeError("bucket_map must have 64 rows; run m6/setup_shards.py")
        unknown = set(self.bucket_map.values()) - set(self.pools)
        if unknown:
            raise RuntimeError(f"bucket_map names shards without a URL: {unknown}")

    def shard_for(self, user_id: uuid.UUID | str) -> str:
        return self.bucket_map[shard_bucket(user_id)]

    @contextmanager
    def conn(self, shard: str):
        with self.pools[shard].connection() as c:
            yield c

    def close(self) -> None:
        for p in self.pools.values():
            p.close()


@lru_cache
def get_router() -> Router:
    return Router()
