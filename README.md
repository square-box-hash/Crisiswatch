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

Free Render instances have no shell, so the same thing is available over HTTP:

```bash
curl -X POST "https://YOUR-API/admin/migrate?secret=$CRON_SECRET&dry_run=true"   # preview
curl -X POST "https://YOUR-API/admin/migrate?secret=$CRON_SECRET"                # run it
```

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
| `db_error: ... DNS query name does not exist` | the Atlas cluster in `MONGODB_URI` has been deleted or renamed. Create a cluster, copy the new connection string, update the env var |
| `db_error: ... authentication failed` | wrong user/password in `MONGODB_URI`, or the password was never URL-encoded |
| `db_error: ... timed out` | Atlas Network Access does not allow `0.0.0.0/0` |

A dead database no longer stops the API from booting: it starts, `/health`
returns 200 with `db_connected: false` and the reason in `db_error`, and data
endpoints answer 503 with that same message.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/` | liveness |
| GET | `/health` | DB, keys, counts, last run |
| GET | `/outbreaks` | `?days=30&region=Zambia&q=cholera&severity=critical,high&status=confirmed&limit=50` |
| GET | `/outbreaks/{id}` | full document incl. `snapshots` |
| GET | `/outbreaks/{id}/summary` | AI summary + worker consensus |
| GET | `/stats` | sidebar counts, totals, countries |
| POST | `/outbreaks/detect` | upsert one incident — needs secret |
| DELETE | `/outbreaks/{id}` | remove one outbreak — needs secret |
| GET | `/trigger-pipeline` | run the pipeline in the background — needs secret |
| POST | `/workers/register` | health worker signup |
| POST | `/workers/{id}/verify` | needs secret |
| POST | `/outbreaks/{id}/review` | worker review + consensus |
| POST | `/admin/migrate` | backfill legacy documents — needs secret, `&dry_run=true` to preview |

Writes take the secret either as `?secret=` or an `x-cron-secret` header.

Severity is one vocabulary end to end: `low | moderate | high | critical`.

---

## Data model

Counts are **tri-state**. `cases: null` means no source stated a figure — it is
never rendered as 0, and a later report that states no figure cannot overwrite
a number another source did state.

```json
{
  "incident_key": "ebola-bundibugyo:kenya",
  "disease": "Ebola (Bundibugyo)",
  "country": "Kenya",

  "estimated_cases": 635,
  "estimated_deaths": 127,
  "cases_unknown": false,
  "cases_source": "https://www.who.int/emergencies/disease-outbreak-news/item/...",
  "cases_as_of": "2026-11-18T00:00:00Z",
  "peak_cases": 635,

  "status": "confirmed",
  "sources_agreeing": 3,
  "severity": "critical",

  "evidence": [
    { "source_name": "WHO DON", "url": "https://...", "tier": "HIGH",
      "cases": 635, "deaths": 127, "published_at": "2026-11-18T00:00:00Z" },
    { "source_name": "CDC Travel Notices", "url": "https://...", "tier": "HIGH",
      "cases": null, "deaths": null }
  ],

  "metadata": {
    "synthesis": {
      "agreement": "partial",
      "contradictions": ["WHO reports 635 cases; ReliefWeb reports 520."],
      "confidence": 0.82
    }
  },

  "snapshots": [
    { "date": "2026-11-19", "cases": 600 },
    { "date": "2026-11-21", "cases": 635 }
  ]
}
```

### Status — how sure, not how bad

Derived from the evidence ledger, never from the model's opinion:

| Status | Rule | Meaning |
|---|---|---|
| `confirmed` | ≥2 independent sources, ≥1 official, with a stated count | corroborated |
| `probable` | 1 official source with a stated count | single authoritative report |
| `suspected` | ≥2 sources, but no official count | exists, unquantified |
| `signal` | 1 source, no count (a travel notice, a listing mention) | worth watching |

`severity` answers "how bad"; `status` answers "how sure". `is_active` in the
API response means the outbreak was seen in the last 14 days.

### Cross-source synthesis

When an outbreak has two or more pieces of evidence, one extra Gemini call
reads them **together** and returns agreement, contradictions and a combined
briefing. It may not invent figures: `synthesis.normalise_result()` drops any
number whose source URL is not in the evidence set. Headline counts are chosen
deterministically in `assess.py` (official sources first, highest stated
figure), so every number in the record traces to a URL.

### Research (optional, Tavily)

Some records arrive with no figure at all — a travel notice says "yellow fever
is present in Ghana" and stops there. When `TAVILY_API_KEY` is set, the pipeline
spends a search on exactly those records:

1. it searches `<disease> <country> outbreak confirmed cases <year>`;
2. fetches the top results (official domains keep their HIGH tier, everything
   else is LOW);
3. runs the normal Gemini extraction over each page;
4. keeps only results that match **both** the disease and the country, then
   recomputes the figures and status from the enlarged evidence ledger.

So a `signal` record can become `probable` or `confirmed` inside one run — but
only because a new URL was actually read, never because the model was asked to
guess.

Cost: one search credit per record, plus one Gemini call per page.

**Budget it deliberately.** A record that stays unknown would otherwise be
re-searched on every run, and at a 30-minute schedule that is 48 runs a day —
4,300 searches a month against a 1,000-credit free plan. Two guards prevent it:

- `RESEARCH_COOLDOWN_HOURS=24` — a record searched in the last 24 hours is
  skipped, whether or not the search found anything;
- `MAX_RESEARCH_PER_RUN=2` — a hard cap per run.

That works out to a handful of searches a day in steady state. The free plan
hard-stops when credits run out; it will not bill you.

Leave `TAVILY_API_KEY` empty to disable the whole step.

### GDELT (optional)

`ENABLE_GDELT=1` adds news articles as a low-reputation corroboration source.
Off by default: it is noisy and costs one Gemini call per article.

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
