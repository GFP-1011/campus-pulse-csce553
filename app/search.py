"""M5 - full-text post search.

Runs the SQL function created by sql/m5_search.sql (PostgreSQL only).
All user input is sent as bound parameters; nothing is formatted into SQL.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as DbSession

from app.auth import get_current_user
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/api")

PAGE_SIZE = 20
MAX_PAGE = 50

SEARCH_SQL = text(
    """
    select * from search.search_posts(
        CAST(:q AS text), CAST(:author AS integer), CAST(:tag AS text),
        CAST(:since AS timestamptz), CAST(:sort AS text),
        CAST(:lim AS integer), CAST(:off AS integer))
    """
)
FACETS_SQL = text(
    """
    select * from search.search_tag_facets(
        CAST(:q AS text), CAST(:author AS integer), CAST(:since AS timestamptz), 8)
    """
)


class SearchAuthor(BaseModel):
    id: int
    display_name: str


class SearchHit(BaseModel):
    id: int
    body: str
    author: SearchAuthor
    created_at: datetime
    like_count: int
    tags: list[str]
    rank: float


class TagFacet(BaseModel):
    tag: str
    n: int


class SearchResponse(BaseModel):
    q: str
    sort: str
    page: int
    page_size: int
    total: int | None
    results: list[SearchHit]
    facets: list[TagFacet]
    db_ms: float


@router.get("/search", response_model=SearchResponse)
def search_posts(
    q: str = Query(min_length=1, max_length=200, description="Words, \"a phrase\", or, -exclude"),
    author_id: int | None = Query(default=None, ge=1),
    tag: str | None = Query(default=None, max_length=50, pattern=r"^#?\w+$"),
    since: datetime | None = None,
    sort: Literal["relevance", "newest"] = "relevance",
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
    facets: bool = True,
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    del current_user  # search is available to every signed-in user (single public feed)
    q = q.strip()
    if not q:
        raise HTTPException(status_code=422, detail="Search text is required")
    tag = tag.lstrip("#").lower() if tag else None
    params = {
        "q": q, "author": author_id, "tag": tag, "since": since, "sort": sort,
        "lim": PAGE_SIZE, "off": (page - 1) * PAGE_SIZE,
    }
    t0 = time.perf_counter()
    try:
        rows = db.execute(SEARCH_SQL, params).mappings().all()
        tag_rows = (
            db.execute(FACETS_SQL, {"q": q, "author": author_id, "since": since}).mappings().all()
            if facets else []
        )
    except DBAPIError as exc:  # e.g. search schema not created yet
        db.rollback()
        raise HTTPException(status_code=503, detail="Search is not available") from exc
    db_ms = (time.perf_counter() - t0) * 1000

    results = [
        SearchHit(
            id=r["id"], body=r["body"],
            author=SearchAuthor(id=r["author_id"], display_name=r["author_name"]),
            created_at=r["created_at"], like_count=r["like_count"],
            tags=list(r["tags"] or []), rank=r["rank"],
        )
        for r in rows
    ]
    # total comes from count(*) over (); a page past the end has no rows to carry it.
    total = rows[0]["total"] if rows else (0 if page == 1 else None)
    return SearchResponse(
        q=q, sort=sort, page=page, page_size=PAGE_SIZE, total=total, results=results,
        facets=[TagFacet(tag=r["tag"], n=r["n"]) for r in tag_rows], db_ms=round(db_ms, 2),
    )
