# How the Project Is Used

Three kinds of people use this project: **users** who collect business data,
**operators** who run the server, and **developers** who change the code.
This guide walks through each, then shows how to extend the code.

---

## 1. Users — collecting data in the web app

1. **Open the app** (local: http://127.0.0.1:8100) and **sign up / log in**.
   Your records, Excel files and jobs are private to your account.
2. **API key** — searches use the server's Serper key ("Demo API key") until
   you add your own with the **API key** button. With your own key your
   searches spend your credits and are not limited by the daily demo cap.
3. **Pick a category** (Finance, Law Firms, RPO, …) or type a **custom
   category** (e.g. "Hospitals").
4. **Location** — country, then optionally state and city:
   * state only → searches across that state's cities;
   * city → that city only;
   * each location is its own search: a Prayagraj search never shows or
     counts New Delhi results.
5. **Keywords (optional)** — separate with commas: `CPA, Tax, Business
   development executive`. Multi-word phrases stay together; the hint under
   the box shows how they were understood.
6. **Advanced options** — *Target records* (any number from 1 to 10,000)
   with a live **credit estimate** ("~400 credits, never more than 1,500").
7. **Start.** Watch the progress bar and live activity log
   (`Found: …`, `Email check: … -> Valid (100/100)`, `Rejected: …`).
   You can **Stop** at any time; starting the same location again resumes.
8. **Results** — the table shows this search's records (switch the
   location filter to "All locations" for everything); **download the Excel
   file** for the category.

### Reading the Excel file

* One **sheet per state**; inside it, a coloured **heading per searched
  location**, newest searches below older ones.
* **Record Status** — use *Verified* and *Likely valid* first; check
  *Needs review* by hand; skip *Low quality*.
* **Email Status** — *Valid* (mailbox exists), *Risky* (accept-all domain or
  throwaway address), *Unknown* (could not be confirmed — format and domain
  are fine), *Invalid* (do not use). **Email Check** says exactly which
  check gave which answer; **Email Source** links to where the email was found.
* **Source URL / Source Page** — where the business and its details came from.

---

## 2. Operators — running the server

### Local / office PC

```powershell
cd app
.venv\Scripts\python.exe -m backend.serve --host 127.0.0.1 --port 8100
```

* `start_app.bat` does the same and opens the browser; `start_app_network.bat`
  serves other PCs on the LAN (set `APP_AUTH_TOKEN` first).
* The mailbox (SMTP) email check needs outbound **port 25**. Test it:
  `Test-NetConnection gmail-smtp-in.l.google.com -Port 25` →
  `TcpTestSucceeded : True`.

### Render (cloud)

* `render.yaml` deploys `app/` as a web service; `main` deploys only after
  the GitHub Actions tests pass.
* Set secrets in the Render dashboard: `SERPER_API_KEY`, `SUPABASE_DB_URL`
  (keeps users' data across Render's disk wipes), `APP_AUTH_TOKEN`.
* Render's free plan blocks port 25 → email checks run without the mailbox
  step (the log says so). For full email verification run the app, or a
  copy of it, on a host where port 25 is open.

### Everyday settings (`app/.env`, restart to apply)

| Want to… | Set |
|---|---|
| let admins see per-user credit usage | `ADMIN_EMAILS=a@x.com,b@y.com` (Admin button in the header) |
| allow more simultaneous searches | `MAX_ACTIVE_JOBS=` (default 4; 3 on a small host) |
| cap spending | `MAX_CREDITS_PER_RECORD=` (per run), `MAX_CREDITS_PER_IP_PER_DAY=` (demo key) |
| turn email verification off / only skip the mailbox step | `EMAIL_VERIFY=0` / `EMAIL_SMTP_CHECK=off` |
| reduce "server refused the check" answers | `EMAIL_VERIFY_FROM=verify@yourcompany.com` |
| stop social-profile email lookups (save credits) | `SOCIAL_EMAIL_LOOKUP=0` |
| let small-city searches widen to nearby cities | `SEARCH_NEARBY_CITIES=1` |

### Where to look when something is wrong

* Live: the activity log on the page (every rejection, failure and recovery
  is written there in plain words).
* `app/output/app.log` (API) and `app/output/worker.log` (collection).
* `GET /api/health` → `status`, `worker`, `database`.

---

## 3. Developers — working on the code

### Set-up

```powershell
cd app
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

### Tests (run before every commit — CI runs the same list)

```powershell
cd app
foreach ($t in "test_security","test_auth","test_keys","test_export","test_env_reload",
               "test_persist","test_isolation","test_concurrency","test_credits",
               "test_pipeline","test_performance","test_resilience","test_emailcheck") {
  .venv\Scripts\python.exe tests\$t.py
}
.venv\Scripts\python.exe tests\loadtest\smoke.py
```

The tests simulate Serper, websites and mail servers — no Serper key and no
credits are needed. `test_pipeline.py` is the best starting point to see the
whole pipeline at work.

### Workflow

1. Branch from `main`, make the change, add or extend a test.
2. Run the test list above.
3. Push the branch and open a pull request. Merging to `main` deploys to
   Render once CI passes.
4. Never commit `app/.env` or `app/output/` (both are git-ignored).

---

## 4. Extending the code — where changes go

| Goal | Where to change |
|---|---|
| **Add a fixed category** | `categories.py`: add an entry to `CATEGORIES` (display name, Excel file name, query templates, relevance terms) and its sub-niche phrases in `CATEGORY_PHRASES`. |
| **Add a search engine (e.g. Bing)** | `search.py`: subclass `SearchProvider` (implement `organic`, `organic_page`, and `places`/`places_page` if it has local listings) and return it from `make_provider()`; add its key to `config.py`; allow its name in `main.py` (`"providers"` in `/api/config` and the check in `/api/collect`); add it to the provider dropdown in `index.html`. *(Not implemented yet — only Serper/Google exists today.)* |
| **Add an Excel column** | `exporter.COLUMNS` (order = Excel order), set the value on the record in `engine.crawl_site` / `engine.enrich_place`, update `tests/test_export.py` (filter range). |
| **Change credibility scoring** | `validator.confidence` (points) and `validator.record_status` (labels); tests in `tests/test_emailcheck.py` §8. |
| **Change email verification** | `emailcheck.py` (`_verify` = order of checks, `classify_reply` = how mail-server answers are read); tests in `tests/test_emailcheck.py` use a local fake mail server. |
| **Add cities / states** | `geo.STATE_CITIES` (and `categories.MAJOR_CITIES` for country-wide searches). |
| **Change the Excel layout** | `exporter._layout` (grouping) and `exporter._fill_sheet` (rows, headings, styling); `exporter.read_data_rows` must keep counting only record rows. |
| **New API endpoint** | `main.py` route → if it needs collection data, a `cmd_<name>` method in `worker.py`, called with `await hub.call("<name>", …)`. |

---

## 5. Known limits

* Only **Google (via Serper)** is implemented as a search source; Bing would
  be a new provider (see the table above).
* Social networks are not crawled; emails are read from Google's public
  result for the business's own profile page, so only emails visible there
  are found.
* The SMTP mailbox check needs port 25; accept-all domains can never be
  confirmed mailbox-by-mailbox (by any tool).
* A website that loads its content with JavaScript only may show no
  details to the crawler.
