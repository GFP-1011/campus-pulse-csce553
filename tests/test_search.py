"""M5 search tests.

Input validation runs on the default SQLite test database.
Known-answer tests need PostgreSQL (full-text search) and run only when
M5_TEST_DATABASE_URL points at an empty, disposable Postgres database, e.g.
  M5_TEST_DATABASE_URL=postgresql+psycopg://postgres:pw@localhost:5432/m5test pytest tests/test_search.py
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app

SQL = (Path(__file__).resolve().parents[1] / "sql" / "m5_search.sql").read_text()
PG_URL = os.environ.get("M5_TEST_DATABASE_URL")


def test_search_requires_login(client):
    assert client.get("/api/search?q=campus").status_code == 401


@pytest.mark.parametrize("query", ["", "q=", "q=%20%20", "q=" + "x" * 201, "q=a&page=0", "q=a&page=51", "q=a&tag=bad%20tag"])
def test_search_rejects_bad_input(client, alice, query):
    _, headers = alice
    assert client.get(f"/api/search?{query}", headers=headers).status_code == 422


@pytest.fixture()
def pg_client():
    if not PG_URL:
        pytest.skip("set M5_TEST_DATABASE_URL to run PostgreSQL search tests")
    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        conn.execute(text("drop schema if exists search cascade"))
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(SQL)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        with TestingSession() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        def user(email, name):
            r = c.post("/api/auth/register", json={"email": email, "password": "test-pass-123", "display_name": name})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        alice = user("alice@example.com", "Alice Broussard")
        bob = user("bob@example.com", "Bob Thibodeaux")
        posts = [
            (alice, "Study group for the database exam in the library tonight #finals"),
            (alice, "The library is too loud today, looking for a quiet room"),
            (bob, "Volleyball game at the rec center #gameday"),
            (bob, "Group study? No, just studying alone at the coffee shop"),
            (alice, "Running trails behind campus are open again"),
        ]
        ids = [c.post("/api/messages", headers=h, json={"body": b}).json()["id"] for h, b in posts]
        with engine.begin() as conn:
            conn.execute(text("refresh materialized view search.post_search"))
        yield c, alice, ids
    app.dependency_overrides.clear()
    with engine.begin() as conn:
        conn.execute(text("drop schema if exists search cascade"))
    Base.metadata.drop_all(bind=engine)


def search(c, headers, **params):
    r = c.get("/api/search", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_known_answers(pg_client):
    c, h, ids = pg_client
    hit_ids = lambda d: sorted(x["id"] for x in d["results"])  # noqa: E731
    # all words must match
    assert hit_ids(search(c, h, q="library exam")) == [ids[0]]
    # phrase search: only the post with the adjacent words "study group"
    assert hit_ids(search(c, h, q='"study group"')) == [ids[0]]
    # stemming: "studies" matches "study" and "studying"
    assert hit_ids(search(c, h, q="studies")) == sorted([ids[0], ids[3]])
    # exclusion
    assert hit_ids(search(c, h, q="library -quiet")) == [ids[0]]
    # or
    assert hit_ids(search(c, h, q="volleyball or trails")) == sorted([ids[2], ids[4]])
    # joined author name is searchable (weight B)
    assert hit_ids(search(c, h, q="thibodeaux")) == sorted([ids[2], ids[3]])
    # nothing
    d = search(c, h, q="zeppelin")
    assert d["total"] == 0 and d["results"] == []


def test_filters_facets_and_ranking(pg_client):
    c, h, ids = pg_client
    d = search(c, h, q="library")
    assert d["total"] == 2
    assert d["results"][0]["id"] == ids[0] or d["results"][0]["rank"] >= d["results"][1]["rank"]
    # hashtag facet counts describe the current text match
    assert {"tag": "finals", "n": 1} in search(c, h, q="study")["facets"]
    # tag filter narrows the results
    assert [x["id"] for x in search(c, h, q="study", tag="#finals")["results"]] == [ids[0]]
    # newest sort
    newest = search(c, h, q="library", sort="newest")["results"]
    assert newest[0]["created_at"] >= newest[1]["created_at"]


def test_search_uses_gin_index(pg_client):
    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        conn.execute(text("set local enable_seqscan = off"))
        plan = "\n".join(r[0] for r in conn.execute(text(
            "explain select id from search.post_search where fts @@ websearch_to_tsquery('english', 'library')")))
    assert "post_search_fts" in plan
