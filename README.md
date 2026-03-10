# Hasan Tutoring Site

This repository now contains an Astro project that recreates the Hasan Tutoring homepage as a static site.

## Getting Started

```bash
cd hasantutoring-site
npm install          # installs Astro and dependencies
npm run dev          # starts a local dev server
npm run build        # creates the production build
```

Without Node installed locally you can still inspect the source – everything lives under `hasantutoring-site/src`.

## Flask + Postgres Backend

Python dependencies are defined in `requirements.txt`:

- `Flask`
- `gunicorn`
- `psycopg2-binary`

Backend entrypoint: `backend/app.py`

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set your Postgres connection in `.env` via `DATABASE_URL` (or `PG*` vars).

### Run (development)

```bash
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
flask --app backend.app run --debug
```

### Run (production style)

```bash
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
gunicorn backend.app:app
```

### API endpoints

- `GET /api/health` checks API + DB connectivity
- `GET /api/tests` returns `id` and `name` rows from `tests` table
