# Scraper Marketing Automation — Business Data Collector

A multi-user web app that finds businesses through Google (via the
[Serper.dev](https://serper.dev) API), crawls their public websites and
official social media profiles, extracts contact details, **validates every
email address**, removes duplicates, scores each record's credibility, and
delivers clean, location-organised **Excel files**.

```
SEARCH ──> DISCOVER ──> CRAWL ──> EXTRACT ──> SOCIAL FALLBACK ──> VALIDATE ──> DEDUPLICATE
   ──> EMAIL VERIFICATION ──> CREDIBILITY SCORE ──> EXCEL (one sheet per state, one heading per search)
```

---

## Features

| Feature | What it does |
|---|---|
| **Multi-user** | Accounts with login; every user has their own records, jobs, Excel files and (optionally) their own Serper key. Several users collect at the same time; extra jobs queue. |
| **Multi-source** | Google organic results + Google Places (Maps) listings, business websites (homepage + contact pages), directory/list pages, and the business's **own Facebook / Instagram / X / LinkedIn** profiles when its website shows no email. |
| **Advanced search** | Fixed categories (Finance, Law Firms, RPO, …) or any custom category; **multiple keywords**, including multi-word phrases (`Business development executive`); **custom record targets from 1 to 10,000** with a live Serper-credit estimate. |
| **Location-based** | Country / state / city search. Each search location is its own search: own target, own results, never mixed with an earlier search in another place. |
| **Excel management** | One workbook per category, **one sheet per state**, a **heading per searched location** (a later city search is appended below the state's results), duplicates blocked, existing records never altered or removed. |
| **Email validation** | Format → domain → DNS/MX → **SPF** → **SMTP mailbox check** (no email is ever sent), plus accept-all and throwaway-address detection. |
| **Data credibility** | Confidence Score (0–100), Record Status (Verified / Likely valid / Needs review / Low quality), Email Status / Score / Check, Email Source, Source URL, Source Page. |
| **Credit control** | Every paid search is logged; a search already paid for is never paid for again; per-run and per-network daily credit caps; admin usage report. |

---

## Quick start (Windows, local)

Requirements: **Python 3.10+** (tested on 3.12) and a Serper.dev API key.

```powershell
cd app
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # then edit .env: SERPER_API_KEY=your_key
.venv\Scripts\python.exe -m backend.serve --host 127.0.0.1 --port 8100
```

Open **http://127.0.0.1:8100**, sign up / log in, pick a category, enter a
location and keywords, set the record target, press **Start**.
(`app\start_app.bat` does the same with one double-click.)

macOS / Linux: same steps with `.venv/bin/python`.

---

## Output

Excel files appear in the page's download list (and in `app/output/users/<user>/`):

* `01_Finance.xlsx`, `05_Law_Firms.xlsx`, … — one per category
* `00_Master_Summary.xlsx` — record counts per category

Inside a category file:

```
Sheet "California"
  Company Name | Category | … | Business Email | Email Status | Email Score | Email Check | Email Source | … | Confidence Score | Record Status
  ▌ Search location: California, USA   (120 records)
    … the state search's results …
  ▌ Search location: Los Angeles, California, USA   (35 records)
    … the city search's NEW results, appended below …
Sheet "Texas"
  ▌ Search location: Texas, USA   (80 records)
```

---

## Configuration (`app/.env`)

The most important settings — see [`app/.env.example`](app/.env.example) for all of them.

| Setting | Purpose |
|---|---|
| `SERPER_API_KEY` | Server's Serper key (users may also save their own in the app) |
| `SUPABASE_DB_URL` | Postgres for accounts + saved user data (needed on hosts whose disk is wiped, e.g. Render) |
| `ADMIN_EMAILS` | Accounts that see the admin usage / activity report |
| `MAX_ACTIVE_JOBS` | Collections running at the same time |
| `MAX_CREDITS_PER_RECORD` / `MAX_CREDITS_PER_IP_PER_DAY` | Spending safety caps |
| `EMAIL_VERIFY`, `EMAIL_SMTP_CHECK`, `EMAIL_VERIFY_FROM` | Email verification on/off, mailbox-check mode (`auto` / `on` / `off`), probe sender |
| `SOCIAL_EMAIL_LOOKUP` | Look up emails on the business's own social profiles (1 credit each) |
| `SEARCH_NEARBY_CITIES` | Let a city search widen to nearby cities (off by default) |

> **Port 25:** the mailbox (SMTP) check needs outbound port 25. Most home
> connections allow it; Render's free plan and most clouds block it — the app
> detects this and skips only that step (format, domain, MX, SPF and
> throwaway checks still run).

---

## Deploy (Render)

[`render.yaml`](render.yaml) defines the web service. Set `SERPER_API_KEY`,
`SUPABASE_DB_URL` and `APP_AUTH_TOKEN` in the Render dashboard. A commit to
`main` deploys only after the GitHub Actions test suite
([`.github/workflows/tests.yml`](.github/workflows/tests.yml)) passes.
Details: [`app/DEPLOYMENT.md`](app/DEPLOYMENT.md).

---

## Tests

```powershell
cd app
.venv\Scripts\python.exe tests\test_pipeline.py      # end-to-end pipeline (simulated web)
.venv\Scripts\python.exe tests\test_emailcheck.py    # email verification (fake mail server)
.venv\Scripts\python.exe tests\test_export.py        # Excel layout + data preservation
```

All suites run in CI; none needs a Serper key or network access to real sites.
The full list is in [`.github/workflows/tests.yml`](.github/workflows/tests.yml).

---

## Documentation

| Document | Read it for |
|---|---|
| [`docs/CODE_UNDERSTANDING.md`](docs/CODE_UNDERSTANDING.md) | Architecture, every module's job, the record data model, storage, security |
| [`docs/CODE_FLOW.md`](docs/CODE_FLOW.md) | Step-by-step flow of a search, with diagrams — from the Start button to the Excel row |
| [`docs/HOW_THE_PROJECT_IS_USED.md`](docs/HOW_THE_PROJECT_IS_USED.md) | How users, operators and developers use the project day to day, and how to extend it |
| [`app/README.md`](app/README.md) | Detailed operator notes (keys, admin report, performance) |
| [`app/SECURITY.md`](app/SECURITY.md) | Security model |

---

## Project layout

```
.
├── README.md                    ← this file
├── docs/                        ← code understanding, code flow, usage guide
├── render.yaml                  ← Render deployment blueprint
├── business_collector.py        ← original single-file CLI collector (legacy)
├── .github/workflows/tests.yml  ← CI test suite
└── app/
    ├── backend/                 ← FastAPI API + collection worker
    │   ├── main.py, serve.py, hub.py, worker.py, auth.py, keys.py, db.py, persist.py, config.py
    │   └── collector/           ← search, crawl, extract, validate, dedup, email check, Excel
    ├── frontend/index.html      ← the whole single-page web UI
    ├── tests/                   ← test suites + load tests
    ├── requirements.txt
    └── .env.example             ← configuration template (copy to .env)
```

## License

See [LICENSE](LICENSE).
