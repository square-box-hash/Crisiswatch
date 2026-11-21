# CrisisWatch — backend

AI-powered disease outbreak surveillance. Reads WHO, CDC, ECDC, ReliefWeb and
ProMED, extracts case counts with Gemini, and tracks each outbreak in MongoDB
over time.

Frontend: [Crisiswatch_frontend](https://github.com/square-box-hash/Crisiswatch_frontend)

---

## How a run works

```
  sources.py                fetch.py              extract.py           store.py
┌──────────────┐        ┌──────────────┐      ┌──────────────┐    ┌──────────────┐
│ RSS / JSON   │        │ article page │      │ Gemini reads │    │ upsert on    │
│ API / HTML   │───────▶│  → clean text│─────▶│ the article  │───▶│ incident_key │
│ index        │        │              │      │ → JSON items │    │ + snapshot   │
└──────────────┘        └──────────────┘      └──────────────┘    └──────────────┘
   candidate                article              incidents            MongoDB
     URLs                                                              ↓
                                                              GET /outbreaks
```

The important change from v4: **the pipeline reads article pages, not listing
pages.** A listing page carries headlines; the case counts live inside the
article. Reading the listing was why extraction kept returning nothing.

One document per outbreak, keyed on `incident_key` = `disease:country` (no date
in the key). Re-running the pipeline updates that document and appends one
`snapshots` entry per day, so case counts form a time series instead of being
overwritten or duplicated.

---

## Sources

| Source | Type | Reputation |
|---|---|---|
| WHO Disease Outbreak News | item pages from the DON listing | 0.98 |
| CDC Travel Notices | RSS → notice pages | 0.95 |
| ECDC threats | threat pages | 0.94 |
| ReliefWeb | v1 `/reports` API (epidemic filter) | 0.92 |
| ProMED | feed, falling back to the posts index | 0.90 |
| Outbreak News Today | RSS | 0.72 |
| WHO AFRO | listing text | 0.96 |
| HealthMap | listing text | 0.78 |

Every source is independent: one failing logs a warning and the run continues.
`pipeline_runs` in Mongo records what each source returned.

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in the three required values
```

Required in `.env`:

| Variable | Where to get it |
|---|---|
| `MONGODB_URI` | MongoDB Atlas → Connect → Drivers |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `CRON_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

---

## Running

```bash
# API
uvicorn api:app --reload

# one pipeline run
python pipeline.py --once

# extract but write nothing (no Mongo needed — still needs Gemini)
python pipeline.py --dry-run --limit 5

# run every 30 minutes
python pipeline.py --interval 30
```

On Render, point a cron job (or UptimeRobot) at:

```
GET /trigger-pipeline?secret=$CRON_SECRET
```

---

## Upgrading an existing database

Documents written by v4 have no `incident_key`, no `last_seen` and no
`snapshots`, so the new API (which filters on `last_seen`) would not show them.
Run the backfill once after deploying:

```bash
python scripts/migrate_v5.py          # dry count
python scripts/migrate_v5.py --yes    # backfill, merge duplicates, create indexes
```

It is idempotent and merges rows that map to the same outbreak.

---

## Is it actually live? Check these three things

1. **`GET /health`** — `db_connected`, `gemini_key`, how many outbreaks are
   stored, and the last run's status with its errors.
2. **`GET /stats`** — counts by severity, totals, countries, `last_run`.
3. **`pipeline_runs` in Mongo** — one document per run: per-source candidate
   counts, articles fetched, items extracted, incidents created/updated, and an
   `errors` array.

If a run produces nothing, the run document says which stage failed:

| What you see | Meaning |
|---|---|
| `no_sources` | every feed returned zero candidates — check the `[Source]` lines |
| `no_articles` | candidates found, but article pages returned no text |
| `no_outbreaks` | Gemini worked, nothing qualified as an active outbreak |
| `Gemini client unavailable` | `GEMINI_API_KEY` missing or SDK not installed |
| `Model not found` / `404` in errors | `GEMINI_MODEL` is not available to your key |
| `429` / `RESOURCE_EXHAUSTED` | quota — lower `MAX_ARTICLES_PER_RUN` or `GEMINI_CONCURRENCY` |

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/` | liveness |
| GET | `/health` | DB, keys, counts, last run |
| GET | `/outbreaks` | `?days=30&region=Zambia&q=cholera&severity=critical,high&limit=50` |
| GET | `/outbreaks/{id}` | full document incl. `snapshots` |
| GET | `/outbreaks/{id}/summary` | AI summary + worker consensus |
| GET | `/stats` | sidebar counts, totals, countries |
| POST | `/outbreaks/detect` | upsert one incident — needs secret |
| DELETE | `/outbreaks/{id}` | remove one outbreak — needs secret |
| GET | `/trigger-pipeline` | run the pipeline in the background — needs secret |
| POST | `/workers/register` | health worker signup |
| POST | `/workers/{id}/verify` | needs secret |
| POST | `/outbreaks/{id}/review` | worker review + consensus |

Writes take the secret either as `?secret=` or an `x-cron-secret` header.

Severity is one vocabulary end to end: `low | moderate | high | critical`.

---

## Data model

```json
{
  "incident_key": "cholera:zambia",
  "disease": "Cholera",
  "country": "Zambia",
  "estimated_cases": 412,
  "estimated_deaths": 11,
  "peak_cases": 412,
  "severity": "high",
  "spread_risk": "high",
  "lifecycle_stage": "active",
  "claim_type": "official",
  "confidence_score": 0.81,
  "verification_status": "unverified",
  "source_urls": ["https://www.who.int/..."],
  "source_names": ["WHO DON"],
  "first_seen": "2026-11-19T04:10:00Z",
  "last_seen": "2026-11-21T04:10:00Z",
  "snapshots": [
    { "date": "2026-11-19", "cases": 380, "deaths": 9 },
    { "date": "2026-11-20", "cases": 400, "deaths": 10 },
    { "date": "2026-11-21", "cases": 412, "deaths": 11 }
  ]
}
```

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

60 offline tests: feed parsing (WHO/CDC/ECDC/ReliefWeb/ProMED fixtures), Gemini
response handling, incident building and severity, Mongo upsert and snapshot
history (mongomock), and the API contract. No network, no keys.

```bash
# against a deployed instance
python scripts/smoke_test.py --base https://crisiswatch-ohrl.onrender.com --secret $CRON_SECRET
```

---

## Housekeeping

```bash
python seed_data.py --yes    # 3 demo rows, marked demo:true — for UI work
python cleanup.py --yes      # delete every outbreak
```

`seed_data.py` is not live data. If the dashboard shows the same rows every
day, check `/health` → `last_run`: the pipeline is not running, or it is
failing before the store step.
