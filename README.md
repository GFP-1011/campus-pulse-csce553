# Campus Pulse

Campus Pulse is a small social-media application built for **CSCE 553 M1 — Baseline System (Option A)**. Users can register, log in, find and add friends, publish and edit messages, read the feed, and like messages. The browser UI and external clients use the same JSON API. This is a class baseline, **not a production service**, and it contains fake data only.

## Architecture

```text
Browser or curl
      │ HTTPS / JSON
      ▼
FastAPI on Render ─────► Supabase PostgreSQL
  ├─ static HTML/CSS/JS
  ├─ authentication API
  └─ social API
```

FastAPI serves both the static UI and the HTTP API. SQLAlchemy sends all persistent data to PostgreSQL. Authentication uses opaque bearer tokens; only a SHA-256 hash of each token is stored in the `sessions` table. Passwords are hashed with Argon2.

## Local development

Python 3.11 or newer is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

For a quick local check, remove or comment out `DATABASE_URL` in `.env`; the application then uses a local SQLite file. SQLite is for development only. The deployed assignment must use PostgreSQL.

Start the server:

```bash
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. Interactive API documentation is at <http://127.0.0.1:8000/docs>.

Run tests:

```bash
pytest -q
```

## Supabase and Render deployment

1. Create a Supabase project dedicated to this course and copy its PostgreSQL connection string. Use the direct connection string when IPv6 is available, or the Supabase session-pooler string otherwise.
2. Create a GitHub repository and push this project. Confirm `.env` is not committed.
3. In Render, create a Blueprint/Web Service from that repository. `render.yaml` supplies the build, start, and health-check settings.
4. Set Render's secret `DATABASE_URL` environment variable to the Supabase connection string. The application accepts both `postgresql://...` and `postgres://...` and selects the psycopg driver automatically.
5. Deploy and check `https://YOUR-APP.onrender.com/health` and `/docs`.
6. Create the three fake demo users. One option is to run the seed command locally while `.env` points to the Supabase database:

```bash
DEMO_USER_PASSWORD='REPLACE_WITH_A_FAKE_PASSWORD' python -m app.seed
```

Alternatively, register each demo account through the deployed UI/API. Never use a real personal password.

The application creates its tables on startup. For a course baseline this keeps deployment small; schema migrations can be introduced in a later milestone when the schema evolves.

## Demo accounts

Replace the password below with the fake password used during seeding before submission.

| Email | Display name | Password |
|---|---|---|
| `alice@example.com` | Alice Demo | `REPLACE_ME` |
| `bob@example.com` | Bob Demo | `REPLACE_ME` |
| `carol@example.com` | Carol Demo | `REPLACE_ME` |

## API

All bodies and responses are JSON. Protected endpoints require `Authorization: Bearer <token>`.

| Method | Path | Auth? | Purpose |
|---|---|---:|---|
| `GET` | `/health` | No | Check that the service is running |
| `POST` | `/api/auth/register` | No | Register and return a bearer token |
| `POST` | `/api/auth/login` | No | Log in and return a bearer token |
| `POST` | `/api/auth/logout` | Yes | Revoke the current token |
| `GET` | `/api/users?q=alice` | Yes | Search users by name or email |
| `POST` | `/api/friends` | Yes | Add a user as a friend |
| `GET` | `/api/friends` | Yes | List friends |
| `POST` | `/api/messages` | Yes | Publish a text message |
| `GET` | `/api/messages` | Yes | Read the message feed |
| `PATCH` | `/api/messages/{id}` | Yes | Edit one's own message |
| `POST` | `/api/messages/{id}/like` | Yes | Like a message (idempotent) |

### Copy-paste curl examples

Set the deployed base URL (with no trailing slash):

```bash
BASE_URL=https://YOUR-APP.onrender.com
```

Health check:

```bash
curl -i "$BASE_URL/health"
```

Register (use a new fake email) or log in:

```bash
curl -s -X POST "$BASE_URL/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","password":"REPLACE_ME"}'
```

Copy the returned `access_token`, then set it without committing it anywhere:

```bash
TOKEN='PASTE_ACCESS_TOKEN_HERE'
```

Write operation—publish a message:

```bash
curl -s -X POST "$BASE_URL/api/messages" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"body":"Hello from the deployed Campus Pulse API!"}'
```

Read operation—list messages:

```bash
curl -s "$BASE_URL/api/messages" \
  -H "Authorization: Bearer $TOKEN"
```

Logout:

```bash
curl -s -X POST "$BASE_URL/api/auth/logout" \
  -H "Authorization: Bearer $TOKEN"
```

## Submission checklist

- [ ] Public HTTPS UI loads outside the developer's laptop.
- [ ] `/health` succeeds against the public URL.
- [ ] GitHub pushes trigger Render deployments.
- [ ] Supabase PostgreSQL is configured; deployed data survives refresh/restart.
- [ ] All three demo users can log in through the UI and API.
- [ ] README passwords and `BASE_URL` placeholders are replaced.
- [ ] No `.env`, database password, real password, or token is committed.
- [ ] All README curl examples work against the public URL.
- [ ] `docs/M1-evidence.md` contains the required screenshot and terminal output.

## Course-use notice

This repository and its deployment are a CSCE 553 class baseline. They are isolated from production systems and use fake users and fake content. They may be load-tested in later course milestones.

