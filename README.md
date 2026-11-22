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

All free, no API key required unless noted.

| Source | Access | Tier | Notes |
|---|---|---|---|
| **WHO DON API** | JSON (`/api/emergencies/diseaseoutbreaknews`) | HIGH 0.98 | structured items with real dates; falls back to the HTML listing if the API is slow |
| WHO DON listing | HTML | HIGH 0.98 | the fallback path above |
| **WHO AFRO RSS** | RSS (`afro.who.int/rss.xml`) | HIGH 0.96 | site-wide feed, so items are pre-filtered on outbreak terms before a Gemini call is spent |
| CDC Travel Notices | RSS → notice pages | HIGH 0.95 | risk levels, rarely case counts |
| ECDC | threat pages | HIGH 0.94 | |
| **Africa CDC (ReliefWeb)** | ReliefWeb API, `source.name = Africa CDC` | HIGH 0.93 | Africa CDC's own reports, via OCHA's index |
| ReliefWeb | ReliefWeb API, `disaster_type = EP` | HIGH 0.92 | situation reports with counts |
| ProMED | feed, falling back to the posts index | HIGH 0.90 | |
| **FCDO Travel Advisories** | gov.uk search API (JSON) | HIGH 0.85 | advisories, not counts |
| Outbreak News Today | RSS | MEDIUM 0.72 | |
| WHO AFRO | listing text | HIGH 0.96 | kept for pages the feed misses |
| HealthMap | listing text | MEDIUM 0.78 | JS-heavy, often yields nothing |
| GDELT | DOC 2.0 API | LOW 0.42 | news corroboration, off unless `ENABLE_GDELT=1` |

### Requested but not added

These were asked for and checked; each is missing for a specific reason, not
an oversight:

| Source | Why not |
|---|---|
| IDSP (India) | published as PDF/Excel bulletins, no machine-readable endpoint |
| data.gov.in | has an API but needs a key and a per-resource ID; the outbreak datasets are heterogeneous CSVs |
| PIB health releases | HTML listings only; Firecrawl would need a paid key — Tavily research already covers this ground |
| EIOS | login-gated (WHO account required) |
| HealthMap API | key required, and access is restricted |
| OpenFDA | drugs, devices and food recalls — not outbreak surveillance |
| disease.sh | COVID-19, influenza and vaccines only |
| Global.health | GitHub line-list CSVs, an import rather than a feed |
| UKHSA | metrics-by-geography API (cases per day per region), not an outbreak feed |
| APIS Asia | dashboard only, no public API |
| Taiwan CDC / Singapore MOH | open-data portals need dataset IDs; no outbreak feed |
| WHO EMRO / SEARO / WPRO | regional pages and newsletters, no stable feed |
| US State Dept advisories | the RSS path (`travel.state.gov/_res/rss/TAsTWRSS.xml`) now returns 404 |

Adding any of these later means writing one parser and one registry entry —
`parse_<name>(payload, cfg, limit)` plus a row in `SOURCES`.

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

## Scheduling — once a day

One run a day is the right shape for this: outbreaks change on the scale of
days, and a daily run costs ~15 Gemini calls instead of ~500.

| Tool | Schedule | Notes |
|---|---|---|
| **cron-job.org** (free) | `30 18 * * *` | UTC → 00:00 IST. Supports real cron expressions. |
| Render Cron Job | `30 18 * * *` | $1/month, UTC. Cleanest if you are already on Render. |
| UptimeRobot (free) | every 1440 minutes | Cannot pin a time of day — it drifts from when you created it. |

Point it at:

```
GET https://YOUR-API/trigger-pipeline?secret=$CRON_SECRET
```

Set the request timeout to **at least 60s**: a free instance that has been idle
for a day needs ~1 minute to wake before it answers.

With a daily run you can raise `MAX_ARTICLES_PER_RUN` back to 12 — quota is no
longer the binding constraint.

## Retention — only quantified records are kept

With `PURGE_UNQUANTIFIED=1` (the default), every run ends with a sweep that
deletes records still carrying no case count:

- **kept:** any record with a stated case count above zero
- **deleted:** `cases: null`, `cases: 0`, or no figure at all — including the
  `signal` rows that come from travel advisories

Each run reports it in the summary and the log:

```
[Purge] removed 4 record(s) with no stated case count: Typhoid Fever/Senegal,
        Malaria/Panama, Polio/Afghanistan, Yellow Fever/Ghana
```

Two consequences worth knowing before you rely on it:

1. **History only accumulates for quantified outbreaks.** A signal deleted today
   and re-detected tomorrow starts a fresh record — new `first_seen`, empty
   `snapshots` — rather than continuing a series.
2. **A record that gains a number later is safe.** Counts are never downgraded,
   so once a record is quantified, later reports that state no figure cannot
   erase it and cannot get it purged.

Set `PURGE_UNQUANTIFIED=0` to keep the signals and filter them in the UI
instead (`GET /outbreaks?status=confirmed,probable`).

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

## Diagnostics — where exactly it failed

Every failure is recorded with three things: the **stage** (collect, fetch,
extract, research, synthesize, store), the **subject** (a source name or a URL),
and a **kind** that says what went wrong. The run document keeps them, so
`GET /runs` and `GET /health` show them after the fact.

The run log ends with one line and, when something failed, the fix:

```
── Run no_sources: 0 new, 0 updated, 0.7s ──
── Failures: collect/disabled ×1, fetch/unavailable ×8 ──
   · disabled: switched off — enable the flag if you want this source
   · unavailable: the remote is down — transient, the next run may succeed
```

| Kind | Means | What to do |
|---|---|---|
| `no_key` | credential missing | set it in the environment |
| `auth` | 401 / 403 | key wrong, expired, or lacks permission |
| `rate_limit` | 429 / quota | slow down or raise the limit |
| `unavailable` | 5xx | remote is down — retry later |
| `not_found` | 404 | the URL moved; update the source config |
| `timeout` | no answer in time | connectivity, or raise the timeout |
| `dns` | hostname does not resolve | wrong URL, or the service is gone |
| `connection` | refused / reset / TLS | network problem |
| `http_error` | other non-200 | read the message for the remote's words |
| `parse` | response arrived, unreadable | the parser needs updating |
| `empty` | worked, returned nothing | check the query or the feed |
| `no_match` | results, none about this case | widen the query |
| `blocked` | refused this client | user agent, geo, or robots |
| `disabled` | switched off by config | enable the flag |

```json
"diagnostics": {
  "headline": "collect/auth ×1, fetch/timeout ×3",
  "counts": { "auth": 1, "timeout": 3 },
  "by_stage": { "collect": { "count": 1, "kinds": { "auth": 1 } } },
  "failures": [
    { "stage": "collect", "subject": "ECDC", "kind": "auth",
      "status": 401, "message": "Unauthorized", "url": "https://..." }
  ],
  "advice": [ { "kind": "auth", "advice": "the key is wrong, expired, ..." } ]
}
```

Nothing is swallowed: a source that returns zero candidates now says whether
that was a 403, a timeout, or a page that parsed to nothing.

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
