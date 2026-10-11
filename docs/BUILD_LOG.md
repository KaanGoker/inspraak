# Build log

Each build stage appends a section: what it did, what it verified by running, real numbers,
what is unfinished.

## Stage 1: foundation and data layer (3 October 2026)

### What was built

- Repository scaffold: `.gitignore` first (`.env`, `data/`, `.venv/`, `site/`), `uv` project
  (`pyproject.toml`, Python 3.12, ruff, pytest), `docker-compose.yml` with
  `pgvector/pgvector:pg16` on host port 5544 and `redis:7-alpine` on 6390, `Makefile`
  (`up`, `down`, `migrate`, `ingest`, `ingest-offline`, `embed`, `test`, `lint`, `stats`;
  `precompute` and `export` are stubs for later stages), `.env.example`.
- `inspraak/config.py`: all settings from the environment, one place.
- `inspraak/db.py`: connection with dict rows and pgvector types, process pool for the later
  web and worker processes, plain SQL migrations applied in order from `inspraak/migrations/`
  (`001_init.sql`, `002_publication_initiatives.sql`), `ingest_runs` bookkeeping.
- `inspraak/store.py`: the read side later stages use instead of writing SQL: organisation
  lookup (exact ID, acronym, trigram name), profile vector, past submissions, in-scope
  publications, cosine match of a profile against publications, feedback counts by type and
  country, organisation submissions with the register category joined in, document pages,
  chunk search per publication, meetings per organisation, meetings by subject similarity,
  meeting counts by register category, snapshot stats.
- `inspraak/http.py`: sequential fetcher, one request per second, retries with backoff on
  429 and 5xx, refuses non-JSON answers (the portal answers 200 with an HTML shell on a wrong
  path).
- `inspraak/ingest/cache.py`: on-disk raw cache under `data/raw/hys/`, JSON pages and document
  bytes, freshness window of 20 hours for open data and forever for closed data.
- `inspraak/ingest/register.py`: streaming XML loader with a byte-level sanitiser (XML 1.1
  header and control character references), strict parse, count check against
  `numberOfIR`, upsert that keeps the vector when the profile text did not change, prune of
  registrants that left.
- `inspraak/ingest/hys.py`: listing, client-side scope plan, details, feedback, proposal
  document registration, grouped publications handled once.
- `inspraak/privacy.py`: the guard for feedback rows (names dropped everywhere, citizen rows
  reduced to counting fields, fail closed on unknown types), plus the CHECK constraint in the
  schema as the last line.
- `inspraak/ingest/meetings.py`: XLSX loader, ID splitting that survives commas in names,
  full replace with vectors of unchanged subjects kept.
- `inspraak/pdf.py`, `inspraak/textutil.py`: type sniffing by magic bytes, text per page with
  PyMuPDF, page-bound chunking, cleaning (Postgres refuses NUL), `plain_dashes` for text the
  system writes itself.
- `inspraak/embed.py`, `inspraak/embeddings.py`: fastembed multilingual MiniLM, windowed
  mean for texts over 128 tokens, a deterministic hash embedder for tests, five idempotent
  targets (only NULL vectors are computed; an ingest sets a vector back to NULL when its text
  changes).
- `inspraak/evaluate.py`: matching recall baseline against real past submissions.
- `inspraak/cli.py`: `inspraak db migrate|stats|runs`, `inspraak ingest
  register|meetings|hys|documents|all`, `inspraak embed`, `inspraak org search|show|match`,
  `inspraak consultations`, `inspraak eval-match`.
- Tests: 49, on small recorded fixtures under `tests/fixtures/` (a 17 KB register excerpt
  with the Greenpeace record, a listing page, the detail of initiative 19418, one raw
  feedback page, a 4-row meetings workbook). The DB tests run against a separate
  `inspraak_test` database in the same container and skip when Postgres is down. Repository
  rules are tested too: no long dash or Unicode minus in any project file, `.env` and `data/`
  ignored by git.

### What was verified by running

- Each endpoint re-verified live on 3 October 2026 before the ingest: listing (4,128 rows),
  detail of 19418 (publication 26173, 339 feedback, OPEN), `/api/allFeedback` on 25704
  (7,133 rows, raw row still carries `firstName`, `surname`, `login`), register HEAD
  (116,763,563 bytes), meetings HEAD (922,197 bytes). Details in `docs/DATA.md`.
- Full ingest for the V1 scope, 1 request per second, all from the CLI:
  - Register: 17,897 declared, 17,897 stored, 2,786 with a Belgian head office.
  - Meetings: 11,484 rows, 16,369 organisation links, 15,963 of them in the register export.
  - Have Your Say: 4,109 initiatives from 42 listing pages, 90 details fetched, 81
    publications in scope (41 open and closing by 1 January 2027, 40 closed with 20 or more
    organisation submissions), 31,067 feedback rows (9,143 organisations, 21,924 citizens),
    5 publications truncated at 5,000 rows. The first run took 489 requests; a second run the
    same day hit the cache 489 times and made 11 requests (open feedback pages past the 20
    hour window), which is the resume behaviour working.
  - Documents: 145 proposal documents (143 parsed, 6,889 pages, 27,025 chunks; 2 errors, both
    zip archives on publication 20209). Feedback attachments: 3,271 registered, 3,257 parsed, 0 without
    a text layer, 0 errors, 14 left pending by the cut-off (finished in stage 2), 29,622 pages,
    112,819 chunks.
  - Vectors: 17,897 organisations, 299 publications, 5,906 feedback rows, 11,484 meetings,
    139,844 chunks (all, after the stage 2 resume). About 50 chunks per second on the laptop CPU.
- Privacy: a query for any citizen row with text, organisation or register number returns 0
  rows (the CHECK constraint makes it impossible to insert one; `test_citizen_content_is_rejected_by_the_database`
  proves the constraint fires).
- CLI on real data: `inspraak org search BEUC` finds 9505781573-45; `inspraak consultations
  --open-only` lists the 41 open publications sorted by deadline; `inspraak eval-match` gives
  recall at 5 of 0.425 and recall at 10 of 0.556 against random 0.067 and 0.122 (1,526
  organisations, 2,259 pairs, 81 publications).
- `uv run pytest -q`: 49 passed. `uv run ruff check` and `ruff format --check`: clean.
- The earlier session of this stage was cut off during the attachment pass (at about 2,000
  of 3,271 attachments) and before the last chunk embedding. The resumed run picked up
  from `parse_status = 'pending'` and the NULL vectors, without repeating any request.

### Decisions worth knowing

- Feedback is capped at 5,000 rows per publication (`FEEDBACK_MAX_ROWS`), sorted by date
  ascending, so landscape counts on the five truncated files are counts of the first 5,000
  rows, not of the whole file. The page later must show `feedback_truncated` and
  `total_feedback` side by side.
- Attachment files are not kept on disk by default (about 1.1 GB for the scope); their text is
  in Postgres. `KEEP_ATTACHMENT_FILES=1` keeps them.
- Grouped calls for evidence (one publication under several initiatives) have a link table;
  `publications.initiative_id` stays as the lowest parent so the old joins keep working.
- Source text (feedback, pages) is stored verbatim, long dashes included, so quotes can be
  verified by string match later. Only text the system writes itself goes through
  `plain_dashes`.
- Git is local only, no remote, as the spec asks.

### Unfinished or left for later stages

- No LLM code, no LangGraph, no Celery worker, no web UI, no static export: by design of
  this stage. `docker-compose.yml` has postgres and redis only; api, worker and beat are
  added later.
- `docs/ARCHITECTURE.md` is not written; the graph does not exist yet.
- Optional sources skipped: FTS 2024 funding panel, EP procedure steps.
- 14 in-scope publications have no Commission document (nothing to read for the reader
  node; the dossier summary and the publication title are what there is).
- Attachments that are scanned PDFs come back `empty` and are not OCRed.
- The feedback cap means five closed publications are partial. Raising the cap is a config
  change plus about an hour of polite fetching per 3,600 rows.
- `ingest all` re-downloads the 117 MB register and the meetings file every run; a
  conditional download (ETag or export date check) would save bandwidth on the nightly job.

## Stage 2: agent layer and queue (3 October 2026)

### Picked up from stage 1

- The two background jobs the cut-off had interrupted were resumed first: `ingest documents
  --kind feedback_attachment` processed the remaining 894 pending attachments (894 requests,
  7,521 pages, 29,145 chunks, 0 errors), then `embed --target chunks` started on the
  attachment chunks. Final attachment counts: 3,257 parsed, 0 empty, 0 errors, 29,622 pages,
  112,819 chunks. 14 attachments stay `pending`: they belong to publications 25737 and 25557,
  which left the kept scope, and the ingest skips out-of-scope publications by design.
- The chunk embedding pass finished at 16:26: 80,304 attachment chunks in about 33 minutes
  (about 40 per second), so every chunk now has a vector: 139,844 of 139,844 (27,025 proposal,
  112,819 attachment). The proposal chunks were all embedded before the stage began, so the
  Reader never waited on it. `inspraak db stats` after the pass: documents 3,416, parsed ok
  3,400, pages 36,511, chunks 139,844, chunks embedded 139,844.

### What was built

- `inspraak/llm/`: one provider interface, `complete_json(prompt, schema, purpose, ...)`,
  returning a validated pydantic object. Base class: Redis token bucket (Lua script, per
  minute plus a daily counter, in-memory twin for tests), retries with backoff on 429 and 5xx
  (5, 15, 30, 60 s), one corrective retry on invalid JSON, dash post-filter on every string
  the model returns, `llm_calls` log row per call. `QuotaExhausted` stops a job instead of
  looping. Providers: `gemini` (google-genai, JSON mode with the schema), `anthropic`
  (optional extra, forced tool, no key here so not exercised), `fake` (deterministic and
  purpose-aware: its analyst quotes real sentences plus one invented quote, its drafter adds
  one unsupported sentence, so the verifier and the critic are tested on the real paths).
- Gemini checked with real calls: `models.list` shows 32 generateContent models for the
  key; `gemini-3.5-flash-lite` and `gemini-2.5-flash-lite` both answered a structured probe
  (13 prompt tokens, 19 output tokens each). The pricing page lists both as free of charge.
  Google's rate-limit page no longer publishes numbers (it points to AI Studio); third-party
  pages report 30 requests per minute and 1,000 per day for flash-lite. The project budget
  is `LLM_RPM=10`, `LLM_RPD=300`, shared through Redis by every process. Default model
  `gemini-3.5-flash-lite`.
- Publication layer (`inspraak/jobs/`): Reader summary (one call per publication, cached
  forever in `publication_summaries`), Feedback analyst (one call per organisation
  submission, cached by feedback id in `feedback_analyses`; stance, confidence, up to 3 key
  asks, each quote verified by normalised string match against the source text or dropped;
  confidence under 0.6 or `unclear` goes to the review queue, not to the page; a submission
  with no text and no readable attachment gets no call and an `attachment` review item),
  Meetings mapper (no LLM, subject similarity into `publication_meetings`).
  `precompute_publication` runs the three and stops cleanly on quota.
- Organisation layer (`inspraak/agents/`): LangGraph `StateGraph` with a supervisor that
  routes deterministically on what the state holds, nodes profiler, matcher (pgvector,
  `not_our_topic` corrections exclude and penalise), landscape (cache), lobby (SQL plus the
  meeting map), reader (cached summary plus the chunks closest to the profile), `go_no_go`
  (`interrupt()`), drafter (one call, past submissions as the voice, stored reviewer edits
  as preferences, 4,000 character hard cut), critic (one call, every sentence cited to a
  retrieved chunk or the profile, else removed and reported; a citation outside the
  retrieved set counts as none), `review` (`interrupt()`), memory (`org_memory` rows, draft
  status, version 2 on an edit). Postgres checkpointer (`langgraph-checkpoint-postgres`),
  one trace row per node execution, escalation triggers as review items, service API
  `start_run`, `get_run`, `list_runs`, `list_review_items`, `decide`, `edit_profile`,
  `organisation_memory`, `llm_call_counts`.
- Migration `003_agents.sql`: `llm_calls`, `publication_summaries`, `feedback_analyses`,
  `publication_meetings`, `runs`, `run_traces`, `review_items`, `org_memory`, `drafts`.
- `inspraak/queue.py`: Celery on Redis, `brief` queue (`run_brief`, `resume_brief`, one task
  at a time per worker, queue position in a Redis list, shown by `get_run`), `nightly` queue
  (`nightly_snapshot` 02:30, `precompute_publications` 04:30 Europe/Brussels). The service
  runs inline with `RUN_INLINE=1` and falls back to inline when the broker is down.
- CLI: `inspraak llm probe|calls`, `inspraak precompute`, `inspraak run start|show|list|graph`,
  `inspraak review list|decide|edit-profile`. Makefile: `precompute`, `worker`, `beat`,
  `probe`. `docker-compose.yml`: `worker` and `beat` services under the `workers` profile.
- Docs: `docs/ARCHITECTURE.md` (graph, nodes, interrupts, triggers, tables, provider, queue,
  service API). README updated.
- Tests: 24 new, 73 in total. Provider (schema fill, post-filter, corrective retry, backoff,
  daily quota, call records), memory and Redis buckets (shared, counted), quote verification,
  sentence split and hard limit, decision normalisation, jobs on the small snapshot (summary
  cache, verified and dropped quotes, review queue, no call without text, citizens refused,
  meeting map, precompute), the graph end to end with the fake provider: routing table,
  interrupt at go/no-go, resume with a fresh provider and graph, draft within 4,000
  characters, critic removing the unsupported sentence, interrupt at review, edit decision,
  memory rows, two draft versions, call counts from `llm_calls`, a no-go path, a
  `not_our_topic` correction that empties the next run's matches, profile edits reaching the
  profiler, unknown organisation refused; Celery routes, beat schedule, queue position list.

### What was verified by running

- `uv run pytest -q`: 73 passed. `uv run ruff check` and `ruff format --check`: clean.
- The whole graph with the fake provider on the real snapshot (BEUC), both interrupts and
  both resumes, before the real run; its rows were deleted afterwards so the real cache holds
  Gemini output only.
- Celery for real: a worker (`celery -A inspraak.queue worker -Q brief,nightly`) received and
  finished `run_brief` and `resume_brief` for a fake-provider run started with `inline=False`
  (queue position 1 while queued, then `waiting go_no_go`, then `waiting draft_review`).
  Rows deleted afterwards.
- Real Gemini run, `gemini-3.5-flash-lite`, 16 calls logged in `llm_calls`, 0 failures,
  0 retries, plus the 2 probe calls made before the log existed: 18 calls in total today.
  Tokens logged: 32,632 prompt, 5,343 output. Mean latency 1.2 s (analyst) to 1.8 s (reader).
  - Precompute on three open publications, at most 5 submissions each: 25981 (public
    procurement: summary from 20 chunks plus 5 analyses, 6 calls), 26097 (early warning
    report on waste: summary from 12 chunks plus the single organisation submission,
    2 calls), 25527 (dual-use evaluation: summary from the dossier text, 1 call; its 5
    organisation rows carry no text because the consultation is a questionnaire, so 0
    analyst calls and 5 `attachment / no text` review items). Meetings mapped: 60 per
    publication at similarity 0.45 or more.
  - Quote verification on the 6 real analyses: 17 quotes returned, 17 found verbatim in the
    source, 0 dropped. Stances: 6 `wants_changes`, confidence 0.9 to 1.0, none held back.
  - Run 1, BEUC (9505781573-45), `top_k` 3: matches 25981 (0.634), 25794 (0.608), 25527
    (0.607), all in the match band. Reader: 2 summaries from cache, 1 call for 25794.
    Escalation: `unparseable attachment` on 25527 (wording corrected afterwards to count
    questionnaire rows without an attachment as `no text`, not as an escalation). Decision
    `go 25981`. Drafter 1 call (2,861 prompt, 383 output tokens), draft 1,862 characters,
    stance `wants_changes`. Critic 1 call: 9 of 9 sentences supported, sources `chunk:299`,
    `chunk:1234`, `chunk:1496`, `profile`. Review decision `approve`: memory rows go, no_go,
    no_go, approved, position; run `done`. 3 calls for the whole run.
  - Run 2, Zero Waste Europe (47806848200-34), `top_k` 3: matches 26097 (0.606, match),
    26040 (0.548, uncertain), 25759 (0.488, uncertain); two `relevance in the uncertain band`
    review items. Reader: 1 summary from cache, 2 calls. Decision `go 26097, no_go 26040,
    not_our_topic 25759`. Draft 2,225 characters, `wants_changes`. Critic: 10 of 11
    sentences kept, 1 removed because it cited `chunk:14124`, which was not among the
    retrieved passages; `critic found unsupported claims` review item. The run is left in
    `waiting draft_review` on purpose: that is the "machine draft, awaiting human review"
    state the static export will show. 4 calls for the run.
  - Cost of a cold organisation observed: 2 calls (drafter, critic) plus one shared call
    per matched publication without a cached summary (1 and 2 here). Cache hits on the
    reader: 3 of 6 publication reads across the two runs.
- `inspraak review list` shows the open inbox: 1 blocking `draft_review` (ZWE), 2
  `relevance`, 1 `critic`, 6 `attachment` items. `inspraak llm calls` returns the counts
  above from `llm_calls`.

### Decisions worth knowing

- The matcher's correction reweighting is a fixed penalty (0.3 times the highest cosine
  similarity to any `not_our_topic` publication) on top of exclusion; simple, explainable,
  measured on the test snapshot only.
- The critic is strict by construction: `supported` from the model counts only when the
  cited source is a retrieved chunk id or `profile`. Removed sentences stay in the review
  payload (`machine_text` keeps the uncut draft).
- Interrupt nodes re-run on resume, so they write their blocking review item idempotently
  (one per run and kind) and the trace shows two rows for each interrupt node (pause, then
  resume), which is accurate.
- Fake-provider output never stays in the shared cache next to Gemini output: the smoke
  runs were deleted by provider name. Tests use the separate `inspraak_test` database.
- `.env` keeps `LLM_PROVIDER=fake`; the real runs were made with `LLM_PROVIDER=gemini` on
  the command line, so nothing calls the API by accident.

### Unfinished or left for later stages

- Web (FastAPI, Jinja2, HTMX, port 8010) and the static export: next stage; `make export`
  is still a stub and `docker-compose.yml` has no `api` service yet.
- Demo set: the spec asks for about 40 Belgium-based registrants precomputed; this stage ran
  2 (BEUC, Zero Waste Europe) and precomputed 3 publications with at most 5 submissions
  each, to stay frugal. `make precompute` (open publications, 20 submissions each) and
  `inspraak run start` for the rest are the commands; the daily budget stops a long pass and
  the caches make it resumable.
- Evaluation note: quote verification share (17 of 17) and calls per organisation (3 and 4)
  are real; a hand-checked match precision sample and a cache hit rate over more than two
  runs are still owed.
- The Anthropic provider is written but untested (no key in this project).
- `landscape` shows at most 40 analysed submissions per publication and `by_country` cuts at
  15 rows; the page stage decides the final shapes.
- No git commit existed before this stage; one local commit is made at its end (author

## Stage 3: web UI and static export (3 October 2026)

### Picked up from stage 2

- Nothing of this stage existed yet (no `inspraak/web`, no `api` service, `make export` a
  stub); the working tree was clean at `Inspraak v0.1`. Stage 1 and 2 code was used as is:
  `store`, `service`, `jobs`, `matcher`, `memory`; the only additions outside the new
  modules are the `web` and `export` CLI commands, the Makefile targets, the compose
  services and the docs.

### What was built

- Dependencies: `fastapi` 0.142, `jinja2` 3.1, `uvicorn` 0.54, `python-multipart`;
  `htmx.min.js` 2.0.4 vendored under `inspraak/web/static/` (50,917 bytes), so the app has
  no build step and no CDN.
- `inspraak/web/links.py`: source links. Checked live before writing them: the Have Your
  Say portal is a single-page app and answers 200 for `initiatives/{id}_en`,
  `initiatives/{id}/F{feedbackId}_en` and `initiatives/{id}/feedback_en?p_id={pub}`; the
  register's `organisation-detail_en?id=` page answers 200 with the title "organisation
  detail"; the meetings portal has no per-row or per-organisation deep link (`meeting.do`,
  `search.do`, `organisation.do` all 404, the landing page only lists commissioner, DG and
  agency pages), so meeting rows link to the XLSX file they come from and to the portal's
  commissioner listing. Document links carry a `#page=` anchor.
- `inspraak/web/charts.py`: inline SVG horizontal bars (one hue, value printed next to each
  bar, labels escaped), used for stakeholder type, country and meeting category counts.
  They render in the static export and in print, no script.
- `inspraak/web/views.py`: read-only view models. The organisation page is computed on
  request from the snapshot and the caches, no model call: profile with reviewer
  corrections and the register values next to them, `matcher.match` with the stored
  "not our topic" corrections, sorted by deadline; per match the cached summary, feedback
  counts by declared type and country, the cached analyses with their verified quotes (held
  back, no text and not yet analysed counts shown separately), the meeting map (cache, else
  a read-only similarity pass, both labelled "subject match, approximate"), the five
  proposal passages closest to the profile with page citations, the latest draft row with
  its state, and the go / no-go decision from memory. Consultation, inbox, run and about
  views follow the same pattern. The about view computes the evaluation numbers live
  (quotes kept, critic sentences kept, reader cache hits, calls by purpose).
- `inspraak/web/render.py`: one Jinja2 environment with a `u(kind, id)` helper that builds
  app paths or relative file paths, so the same templates serve the app and the export.
  Timestamps are shown in Europe/Brussels.
- `inspraak/web/app.py`: FastAPI routes (home, search partial, organisation, consultation,
  inbox, run, run status poll, about, healthz) and three actions (start a run, correct the
  profile, decide a review item). `decision_from_form` turns the inbox forms into the
  decision shapes `service.decide` expects: go / no-go / not our topic per consultation;
  approve, edit (an unchanged text counts as approve), reject per draft with the reviewer
  text kept as a preference; seen, "not our topic" (also written to memory so the matcher
  reweights) or a stance correction for the flags. HTMX swaps the decided item in place;
  without HTMX the same forms post and redirect.
- Templates: `base`, `home`, `search`, `organisation`, `consultation`, `inbox`, `run`,
  `about`, partials `landscape`, `lobby`, `draft`, `inbox_item`, `search_results`,
  `run_status`. One stylesheet, system fonts, light paper background, a serif for
  headings, tags for states (match / uncertain, go / no-go, draft states, approximate),
  tables that scroll inside their wrapper on a phone, 16 px gutters, print rules.
- `inspraak/export.py` and `inspraak export --out site/`: index with a client-side search
  over `search.json`, about, one page per organisation with a run (or `--org`), one page
  per in-scope publication, one page per run, `static/style.css`, `.nojekyll`. Relative
  links only, no HTMX, no forms, a notice on every page, drafts with their state label.
- `docker-compose.yml`: `api` service on 8010 (`uv run inspraak web --host 0.0.0.0`,
  healthcheck on `/healthz`, 300 s start period for the first sync), named venv volumes
  for api, worker and beat plus a shared uv cache so restarts are quick. `make web`,
  `make export`. CLI: `inspraak web`, `inspraak export`.
- Tests: 10 new, 83 in total. Web: every page on the small snapshot, the HTMX partial
  versus the full search page, citizen fixture names never on a page, a run started and
  reviewed through the forms end to end (go / no-go, draft review with an edit, run page
  states, organisation page draft state before and after, 409 on a second decision), a
  relevance flag decided "not our topic" that empties the next match list, a profile
  correction through the form, the form to decision mapping, chart escaping, link
  patterns. Export: files written, relative links only, no HTMX, notice present, inbox
  absent, draft state label, search index, an explicit organisation list, no long dash
  in the output.

### What was verified by running

- `uv run pytest -q`: 83 passed. `uv run ruff check` and `ruff format --check`: clean,
  house-rule test included (it caught one long dash the Write tool had expanded from a
  `\u` escape in the new export test; fixed).
- Every route rendered against the real snapshot with the TestClient: home 0.75 s and
  18.7 KB, BEUC organisation page 0.35 s and 112 KB, Zero Waste Europe 0.13 s and 110 KB,
  consultation 25981 33 KB, inbox 18.6 KB, run pages 15 KB, about 8.7 KB, 404s for an
  unknown organisation, publication and run.
- Screenshots with Playwright 1.61 (system install, Chromium headless shell 1243 from the
  browser cache) at 390 and 1280 px, full page, for home, both organisation pages,
  consultation, inbox, run and about, and I looked at them. First pass found two
  horizontal overflows at 390 px (about page 644 px from an unwrapped table, Zero Waste
  Europe 395 px from an unbreakable string), clipped chart labels, a missing space in a
  count line, a two-screen profile sidebar before the consultations on a phone, an
  unwrappable run trace column and UTC timestamps. All fixed and re-shot: no overflow on
  any page at 390 px, no console errors. Four crops were kept under `docs/screenshots/` (moved to `assets/` in the documentation stage).
- Static export on the real snapshot: `inspraak export --out site` wrote 87 pages, 90
  files, 2,213,623 bytes in 1.9 s (2 organisations, 81 consultations, 2 runs). Served with
  `python3 -m http.server`: screenshots at both widths, the JSON search typed "beuc" and
  listed BEUC, the click landed on `org/9505781573-45.html`, the stylesheet loaded, no
  console errors, no `href="/` anywhere, every page carries the static notice and none
  carries the inbox link. `site/` is gitignored.
- Compose: `docker compose up -d --wait api` finished with the api healthy; from the host
  `curl http://127.0.0.1:8010/healthz` answered `{"ok": true, "organisations": 17897}` and
  the full screenshot set above was taken against the container, not the dev server.
  `docker compose --profile workers up -d worker beat`: the worker log shows the four
  tasks registered, `Connected to redis://redis:6379/0` and `celery@... ready`; beat logs
  `beat: Starting...` after installing 108 packages into its volume. Worker and beat were
  stopped again afterwards so beat does not fire the nightly job unasked; postgres, redis
  and api are left running.
- Privacy on the pages: the fixture's invented citizen names never appear in a rendered
  page (test), and the real pages show citizens only as counts in the charts.

### Decisions worth knowing

- Pages are computed on read from the snapshot and the caches rather than from the graph
  checkpoint, so an organisation without a run gets a full page too, and a page never
  calls a model. The only write a GET can cause is none: when a publication has no cached
  meeting map the view runs a read-only similarity pass and says "computed from the
  snapshot" instead of "cached from the nightly map".
- The go / no-go decision reaches the organisation page from memory, which the graph
  writes when the run finishes; while a run waits at draft review the page shows the
  draft with its label but no "reviewer: go" tag yet. The run page shows the decision at
  once.
- Meeting rows link to the source file, not to a portal row: the portal offers no such
  link, and inventing one would break the "every number links to its source" rule.
- Timestamps are shown in Europe/Brussels; dates in the data (deadlines) are shown as
  stored.

### Unfinished or left for later

- Demo set unchanged: 2 organisations with runs and 3 precomputed publications, not the
  roughly 40 Belgian registrants the spec asks for. The export covers whatever has a run,
  so `inspraak precompute` and `inspraak run start` for more organisations grow it without
  code changes; the daily model budget is the limit.
- Evaluation note still partial (no hand-checked match precision sample, cache hit rate
  over two runs); the about page shows the live numbers and says what is missing.
- The static export has no per-organisation "start a run" path by design; the inbox and
  the actions exist only in the app.
- No authentication on the review actions: the app is meant for one reviewer on a
  trusted network or behind a reverse proxy.
- The `api` container runs uvicorn with one worker; fine for the demo, not sized for
  traffic.
- Docker dev-loop note: the compose services sync their environment on first start
  (several minutes with the uv image); the dev server on the host (`make web`) is quicker
  for template work.

## Stage 4: real run and evaluation (3 October 2026)

### Picked up from the cut-off attempt

- The earlier attempt had left uncommitted, working code: `demo_set.csv` (40 Belgian
  registrants), `inspraak demo` / `service.start_demo` (one run per organisation without
  one, resumable, stops on quota), `inspraak eval-report` / `evaluate.report`, the
  precompute walking the whole V1 scope in budget order (`jobs.publications_to_precompute`),
  the analyst counting questionnaire rows without a call, and tests. All kept; 86 tests
  passed on it before anything new.
- Its precompute pass over the open publications had been killed at 32 of 41 after 128
  calls. The Redis day counter read 0 although 144 calls were logged today; it was set
  back to 146 (logged calls plus the 2 unlogged probes) so the budget stayed honest.

### What was run

- `inspraak precompute --open-only --feedback-limit 20`: resumed, 54 calls, 277 s, 41 of 41
  open publications summarised and analysed, 0 failures.
- `make demo`: 38 new runs in 22 s, 0 model calls (every matched summary was cached), 0
  errors; each stops at go / no-go. 40 organisations with a run in total.
- Drafts: go decisions for Transport and Environment (25794, ETS directive), EDRi (26173,
  child safety online) and ETF (26170, training of fishers): 2 calls each, 5 to 6 s each.
  With BEUC (approved) and Zero Waste Europe from stage 2 that is 5 drafts; 4 wait at
  draft review as "machine draft, awaiting human review".
- `make precompute` over the whole scope: 92 calls, 493 s, 45 of 81 publications, then
  stopped cleanly on `daily budget of 300 calls is spent`. Resume command: `make precompute`
  after midnight UTC. Left: 35 closed publications, at most about 735 calls.
- Static export: 163 pages, 166 files, 7.7 MB in 6.6 s (40 organisations, 81
  consultations, 40 runs).

### Bugs fixed

- Logged model latency included the wait for a token from the shared bucket (analyst
  median 5.8 s was the 10 per minute pacing). `Provider.complete_json` now starts the
  clock after the token is granted; after the fix the analyst median is 1.2 s.
- Pages carried long dashes from register names and consultation titles (85 exported
  files). `Renderer.render` and the export's `search.json` now pass through
  `plain_dashes`; new test `test_rendered_pages_carry_no_long_dash`. After the fix: 0 files.
- The staff figure showed float noise ("3.70000004768372 FTE"); rounded to one decimal.

### Evaluation (docs/EVAL.md)

- Hand-checked 30 seeded-random pairs out of the 190 the demo runs produced: 17 relevant
  (57%), 19 counting two borderline pairs. Europol (25397) is a hub: in 21 of 190 lists.
  Scores of relevant and irrelevant pairs overlap; the uncertain band is a review prompt.
- Recall against real filings (`eval-match`): recall at 5 0.425 (random 0.067), MRR 0.301.
- Quotes: 511 of 529 verbatim (96.6%), 18 dropped before storage. Critic: 42 of 44 draft
  sentences supported.
- Calls per organisation: 0 for the brief once publications are precomputed, 2 per draft.
  Reader cache: 193 of 196 reads hit (98.5%).

### Verified by running

- `uv run pytest -q`: 87 passed; `ruff check` and `ruff format --check` clean.
- Static site served with `python3 -m http.server`, Chromium headless at 390 and 1280 px on
  index, about, EDRi, Bruegel, consultation 25794 and the EDRi run page: all 200,
  `scrollWidth` equal to the viewport, no console errors; the JSON search for "transport"
  lists T&E and ETF; the EDRi draft shows its label, 7 of 7 sentences supported and its
  chunk citations. No `href="/` and no long dash in any exported file.
- New screenshot `draft-1280.png` (now `assets/draft.png`).

### Unfinished

- Precompute stopped at 45 of 81 publications by the daily budget (the open ones are all
  done); `make precompute` finishes it over the next days.
- 35 demo runs wait at go / no-go; nobody has reviewed them, so the "not our topic"
  correction was not exercised at scale.
- Matching is the weak point (57% on the sample, generic proposals act as hubs); a
  reranking step or a per-publication score normalisation is the obvious next change.
- Organisation pages are long (about 28,000 px at 1280, 55,000 at 390) because each match
  carries its passages, landscape and meeting table; on a phone the meeting table breaks
  long names mid-word inside its scroll box.

## Stage 5: review fixes (3 October 2026)

All critical and major review findings are fixed. I also fixed most of the minor ones.
Each fix was reproduced first, then tested. Gemini quota: the Redis day counter reads 300
of 300 for 2026-10-03, so this stage made no model calls. The precompute and a new critic
pass wait for the reset.

### Engine fixes

- **"Not our topic" wiped out matches (critical).** Reproduced on real data with the old
  code. Correcting the top match left 28 to 1, 11 to 0, 24 to 0, 6 to 0 and 10 to 0
  matches. Correcting the least similar publication left 28 to 13. The penalty now counts
  only the similarity above a baseline: the mean plus one standard deviation of the
  corrected publication's similarity to the candidates. That excess is scaled to [0, 1]
  and multiplied by 0.3 (`correction_penalty` in `agents/matcher.py`). Same organisations
  after the fix: 28 to 26, 11 to 10, 24 to 23, 6 to 5, 10 to 9. After correcting the
  least similar publication nothing drops. The real corpus has a mean pairwise cosine of
  0.45 (standard deviation 0.13). New `tests/test_matcher.py` runs with the real min_score
  of 0.45. It checks that a near duplicate is still demoted and that typical publications
  get a penalty of 0.
- **A malformed decision broke the run for good.** `service.decide` now validates first,
  with the graph's own `_normalise_go` / `_normalise_review` plus an empty-edit check. A
  non-object is refused too. Nothing is stored for a bad decision and the run keeps
  waiting. New `service.retry_run` / `inspraak run retry <id>` continues a failed run from
  its last checkpoint. A test makes the drafter fail once, then retries; the retried run
  reaches draft review with 1 draft.
- **Double submit race.** The claim is now `UPDATE ... WHERE id = %s AND status = 'open'
  RETURNING id`, and the losing request gets `AlreadyDecided`. The web form maps that to
  409 and other decision errors to 400. Test: two threads behind a barrier give one ok,
  one conflict and one dispatch.
- **No queue position for a resumed run.** Before a Celery dispatch, the run row is set
  to status `queued` with stage `resuming`. Its position then shows and the page polls.
  Test: the dispatch is `celery:brief`, the status is queued, the stage is resuming and
  the position is 3.
- **Sentence splitter.** It now knows abbreviations (Art., No., p., e.g., i.e., Mr.,
  initials) and joins a fragment back when the next one starts in lower case or with a
  digit. The reviewer's example now gives whole sentences (test).
- **The critic accepted evaluations backed only by the profile.** Sentences with stance
  wording ("strongly support", "welcomes", "outweighed") that cite only `profile` are now
  removed by code. Courtesy lines such as "welcomes the opportunity" are allowed. Both
  prompts say the same. This is not yet run on real drafts (no quota), so drafts 4, 6, 7
  and 8 still carry the old sentences. Their pages now say "stance proposed by the model,
  not the organisation".
- **Minor engine fixes:**
  - Quotes: long dashes are normalised on both sides of `quote_found`.
  - Rows stored without text are analysed again once an attachment of theirs parses
    (cache check plus the publication pass query).
  - Deadline clock: `portal_now()` (Brussels) and `PORTAL_NOW_SQL` replace
    `datetime.now()` and `now()` in the matcher, store, views, render, jobs and evaluate.
  - The landscape trace now reads "5 analysed (5 wants changes)", not a Python dict.

### Data and page fixes

- **Demo decisions shown as a reviewer's (critical).** In the database, the 6 decided
  items now say `demo, no human review`: 3 were `kaan (demo)` and 3 were `cli`, the
  stage 4 go decisions. The 4 memory rows and 5 run results were relabelled the same way.
  The draft badge now reads "auto-approved for the demo, no human review", and the chips
  read "demo decision, no human review: go". The `kaan (demo)` text appears 0 times in
  the export.
- **Citations.** "Supported by" now gives the document title and page, with a link,
  instead of `chunk:1234`.
- **6,306 links to pages that do not exist.** `unlink_missing_pages` runs at the end of
  the export. A link to an organisation without a page goes to its register entry, and
  any other missing target becomes plain text. Scan after the export: 1,935 internal
  links, 0 missing.
- **Lobby block.**
  - The text says "The top 60 cabinet meetings by similarity (the list stops at 60)" and
    warns that generic subjects also pass.
  - The chart counts distinct meetings per category and its caption says so.
  - The block is collapsed by default.
  - 80 of 81 publications sit at the cap of 60.
- **Meeting counts.** Identical rows now get a `-2`, `-3` suffix instead of collapsing
  into one meeting. I downloaded today's file (generated 2026-10-03): 11,527 meetings, 682
  newly embedded on the local model. The 81 meeting maps were recomputed. Eurocities 21,
  BusinessEurope 83, FoodDrinkEurope 22 and EuroCommerce 34 now match the live file.
- **Analysis wording.**
  - Pages say "N of M organisation submissions analysed by the model". When no
    submission has text they say "37 organisation submissions, none with readable text,
    so not analysed".
  - The home tile says "organisation submissions stored".
  - Truncated publications say "sample: the earliest 5,000 of 80,159 by date"; the fetch
    sorts by `dateFeedback,ASC`. The chart captions carry the same note.
- **Organisation page.**
  - Cards are ordered by draft first, then by similarity. BEUC now leads with 25981
    (0.63) instead of the export-control consultation.
  - Cards with no document and no readable submission fold into a details element.
  - BEUC page height: 29,049 to 18,073 px at 1280, 57,702 to 39,940 at 390.
- **About page.** It now shows precision 17 of 30 (57%), the Europol hub caveat, recall
  at 5 of 0.43 and a privacy note on sole traders (also in docs/EVAL.md). The "not done
  yet" line is gone.
- **Estimate in docs/EVAL.md.** The old estimate was wrong. 8,375 organisation
  submissions on closed publications are unanalysed (5,673 with text, 592 with an
  attachment only), which is about 6,300 calls, about 21 days at 300 a day. "Done" means
  one capped pass. The open publications have 0 left.
- **Other page fixes:**
  - Inbox: draft reviews first. The go/no-go items and the flags are folded.
  - Run trace: rows stack as cards under 640 px. A note explains why the run's matches
    can differ from the live page.
  - Search: a Belgian head office with an exact acronym or name prefix ranks first. "wwf"
    now gives WWF European Policy Programme first.
  - Static search builds its results with `textContent`, with no `innerHTML`.

### Security

- Compose binds 8010, 5544 and 6390 to 127.0.0.1 (checked with `docker ps` after the
  restart). `POSTGRES_PASSWORD` comes from the environment and defaults to the old value.
  The existing volume keeps its password.
- A middleware guards the app:
  - Optional `REVIEW_TOKEN` basic auth on `/inbox` and every POST.
  - A same-origin check on POSTs (403 on another origin).
  - A per-client limit, `WRITE_RATE_PER_10MIN` (default 20).
- An organisation can have only one queued or running run at a time.
- Tested by `test_write_actions_are_guarded`. The live container answers 403 to a
  cross-origin POST.

### Findings that needed no code change

- **Long dashes in the live app.** The container was running code from before the
  stage 4 render fix. After the restart, `/`, `/inbox`, `/org/9505781573-45` and
  `/consultation/25527` all show 0.
- **Chart `| safe`.** Charts are already escaped, and `test_bar_chart_escapes_and_handles_empty`
  covers it.
- **The Commission mailbox on pages.** It is an institutional address, so no change.

### Verified by running

- `uv run pytest -q`: 99 passed (was 87). `ruff check` and `ruff format --check` are
  clean.
- Export: 163 pages, 166 files, 8.9 MB, 6,306 links unlinked. 0 files with long dashes,
  0 missing internal targets.
- Headless Chromium at 390 and 1280 px, on the BEUC page, consultation 25794 and the
  index:
  - All return 200, with `scrollWidth` equal to the viewport and 0 console errors.
  - BEUC's first card is the public procurement consultation (0.63). The draft badge
    reads "auto-approved for the demo, no human review", and the citations show
    document and page.
  - The search for "transport" lists T&E and ETF. A query with markup renders as text.

### Unfinished

- **Precompute.** About 6,300 analyses are left for the closed publications. Run `make
  precompute` once a day after midnight UTC, about 21 days at 300 a day.
- **The 4 waiting drafts.** They predate the stricter critic. Rejecting them in the inbox
  and running again (2 calls each) would apply it.
- **Matching.** Precision is unchanged (57%). There is no reranking yet, and generic
  meeting subjects still pass at 0.45.
- **A summary typo.** "Consesses" in 25931 is model output. It is labelled as such and
  was not hand-edited.
- **Not done:** CSRF tokens (the same-origin check stands in for them), and changing the
  password of the existing Postgres volume.
- **Not committed.** The working tree has the changes and nothing was pushed. The Redis
  key `inspraak:review:scratch` is left over from the reviewer's test and was left alone.

## Stage 6: documentation and first commit (3 October 2026)

What I did:

- **README.md** rewritten in the house style: hook, features, design notes with the mermaid graph, screenshots, measured results (only numbers from `docs/EVAL.md`), how to run, data sources and limits, project structure, license. The prose went through the humanizer pass.
- **Screenshots** taken from the running app (headless Chromium, 1280 and 390 px, 0 console errors) and saved under `assets/`: organisation, draft, home, inbox, consultation, about, organisation at phone width. The older `docs/screenshots/` were from before the stage 5 fixes and were removed.
- **LICENSE** (MIT, Kaan Goker), `license = "MIT"` in `pyproject.toml`.
- **docs/ARCHITECTURE.md** brought up to the stage 5 code: the correction penalty, the critic's stance rule, decision validation, claim-once decisions, `run retry`, the web guard, Brussels clock, export link cleanup.
- **docs/DATA.md** meetings section corrected to today's file: 11,527 meetings, 16,413 links, 16,007 in the register export, 1,515 multi-organisation rows, and the duplicate-row trap.

What I verified by running:

- `ruff check` and `ruff format --check` clean; `uv run pytest -q` 99 passed.
- **Clean `docker compose up`.** In a separate project name with its own volumes and other ports, a fresh stack failed: the api healthcheck answered 500 (`relation "organisations" does not exist`) because nothing migrated an empty database, so `up --wait` would hang. Fixed: the api command now runs `inspraak db migrate` before starting. Second try: all three containers healthy, `/healthz` `{"ok":true,"organisations":0}`, `/`, `/about`, `/inbox` and `/search` all 200 on the empty database. The test stack and its volumes were removed. The working stack on 8010 was not touched.
- No file over 1 MB is tracked; `git check-ignore` confirms `.env`, `data/`, `.venv`, `site/`.


## Stage 7: backfill with the CLI providers (3 October 2026, evening)

- New providers `claude_cli` (Claude Code CLI, Haiku 4.5 at low effort, `--json-schema`) and `agy` (Antigravity CLI, Gemini 3.8 Flash low, schema in the prompt, model chain), each with tests.
- `inspraak/jobs/batch.py`: parallel threads, 10 submissions per call, opening-words guard per answer, single-call pass for the rest, cache skip. `scripts/backfill.py` chains the passes and the summaries down a provider list.
- Run: Haiku 1,328 submissions in 17 min, then Gemini Flash through agy 4,897 in 34 min (503 batch calls) plus 30 single calls and 35 summaries. 0 failed calls. 0 submissions left in scope, 81 of 81 summaries.
- The 4 drafts written before the stricter critic were rejected and drafted again with Gemini Flash: 29 of 40 sentences kept, 11 removed with reasons.
- Verified: `make test` 104 passed, `make lint` clean, static export rebuilt and scanned (see the commit).

## Matcher v2: one card per initiative, hub penalty, rerank (5 October 2026)

From the launch review (impact lens, change 1): hub files led lists they did not fit, and
one initiative took 2 or 3 of the 5 slots.

- **Hub penalty.** `publications.hub_score` (mean cosine to the 200 nearest registrants,
  centred), 0.5 times subtracted. Grid on `eval-match`: mean-centring and a top 5
  frequency penalty both cost recall; k = 200, weight 0.5 keeps it (recall at 5 0.425 to
  0.429, at 10 0.556 to 0.565). Computed at the end of `inspraak embed` and by
  `inspraak match-calibrate`.
- **One card per initiative** (same initiative id or same short title): the publication
  with a draft, else the open one with the nearest deadline, best score of the group,
  siblings listed on the card.
- **Rerank** (`inspraak/agents/rerank.py`): the 15 strongest cards in one batched call,
  register profile plus title and cached summary, relevance in [0, 1] and a one-line
  reason. Cached in `match_reranks` per organisation, publication and profile hash
  (migration `004_matching.sql`). Under 0.3 dropped, 0.6 and over is the match band. Page
  reads use the cache only; the run's matcher node calls the provider for missing ones and
  falls back to the embedding order on any model error. `MATCH_RERANK=0` turns it off.
  The fake provider has a deterministic `match_rerank` handler.
- A consultation with a draft stays on its organisation page even when the matcher no
  longer ranks it (BEUC's approved procurement draft, now relevance 0.10, with a note).
- `inspraak rerank` fills the cache for a list of organisations; `inspraak demo --replace`
  closes runs that still wait at an undecided go / no-go and have no draft as
  `superseded` (review items decided `{"superseded": true}`, nothing deleted) and starts
  new ones. `scripts/precision_sample.py` draws the hand-check sample reproducibly.

### Run

- `inspraak rerank` for the 40 demo organisations: 40 calls through agy (Gemini 3.8
  Flash low), 0 errors, 558 judgments.
- `inspraak demo --replace`: 35 runs superseded, 35 new runs, 0 model calls, 0 errors. The
  5 runs with drafts were kept. New lists: 162 pairs, 31 distinct consultations (was 24),
  0 lists with two cards of one initiative (was 16 of 35), Europol in 4 lists (was 19).
- `inspraak eval-match --rerank-sample 150`: 150 calls through agy, 0 errors. Per
  initiative on the 150 sampled registrants, recall at 5: embedding 0.453, hub penalty
  0.486, plus rerank 0.651; median rank 6 to 2.
- Hand check, 30 pairs, same seed and rule as on 3 October, judged by the coding agent
  (not blind): **20 of 30 relevant (67%), was 17 of 30 (57%)**. Borderline counted: 25 of
  30, was 19. Same judge on the old 30 pairs: same verdict on all 30. Rank 1 pairs: 8 of 9
  relevant (was 1 of 6). Uncertain band: 0 of 3 relevant.
- Model calls of this stage: 190 `match_rerank` calls, all agy, 0 failed. No Gemini API
  calls, no Claude CLI calls.

### Verified

- `make test`: 111 passed (was 104; 7 new tests for the collapse, the hub score, the
  rerank order and drop, the kept draft, the cache and profile hash, ids the model
  invents, and the demo replace). `make lint` clean.
- api container restarted and healthy; `/about`, `/inbox` and the BEUC, T&E and EDRi
  pages return 200 with no long dash. T&E shows one ETS card with the two siblings listed.

### Unfinished

- Precision 67% is under the 75% the review hoped for. Broad profiles (think tanks,
  consultancies) still get mid-size files the model rates 0.7 or 0.8. Hiding the uncertain
  band would have removed 3 misses from this sample, but that was seen on the test sample
  itself, so it is not applied; it needs a fresh sample to decide.
- The static `site/` export was not rebuilt in this stage; a re-export picks up the new
  lists.
- The live `gemini` provider now spends one rerank call per new organisation (cached). A
  cold organisation with one go costs 3 calls instead of 2.

## Public export made safe for named organisations (6 October 2026)

The launch review found that the static site showed demo go / no-go verdicts and drafts in
the voice of real Brussels organisations, leaked retrieval markers, froze "N days left" at
the export, never said who built it, and quoted numbers that did not match the README.

### What changed

- **Left out of the export.** Drafts, go / no-go decisions and "reviewer:" badges, the
  memory log, review items, "requested by" lines, stance confidence numbers, and cards kept
  only because of a draft. Run pages show the brief stage only, up to the pause for the
  human go / no-go (`views.public_run`); each organisation page links one run, the latest
  that was not superseded. The export clears `org/`, `consultation/` and `run/` first. The
  local app keeps everything.
- **Retrieval markers.** `textutil.strip_chunk_markers` cuts `[chunk:N]`, `[chunk:N,
  chunk:M]`, `(chunk:N p.4)` and bare `chunk:N` from the drafter's text before the critic,
  from the critic's output and when a draft is shown. `scripts/strip_chunk_markers.py`
  cleaned the stored data: 1 draft (EDRi, id 11), 1 run result, 1 review payload. The fake
  drafter now copies markers like a real one, and tests fail if `chunk:` reaches a stored
  draft or an exported page.
- **Critic rule for positions.** A normative sentence in the organisation's voice ("we
  support", "we call for", "must", "should", "it is vital that", or a stance verb with the
  organisation's name or acronym as subject) needs an organisation source: one of its past
  submissions (new citation `submission:<id>`, the critic now sees the excerpts the drafter
  saw) or its register goals (`profile`). Commission passages alone are not enough. A
  sentence that reports what the proposal says ("Under the proposal, providers must ...")
  is exempt. The critic may cite several sources per sentence. Rules in `judge_sentence`
  and `is_normative`, tests in `tests/test_quotes.py`.
- **Identity.** Every page: a slim snapshot bar, a footer "Built by Kaan Goker" with
  kaangoker.dev, the GitHub repository and LinkedIn, "Not affiliated with any organisation
  shown", the data sources with the snapshot date, and a correction address. Description,
  author and Open Graph tags on every page; `robots: noindex, nofollow` on organisation
  pages. The about page opens with who built it and why, and links the source.
- **Front page.** A kicker and a lede that say what it is, then a strip of the agents
  (profiler, matcher, landscape, lobby mapper, reader, human go / no-go, drafter, critic)
  with the supervisor in the caption and a link to the method section.
- **Freshness.** Deadlines are dates with their Brussels offset; a script in the page
  computes the countdown in the reader's browser, says "closed" after the date and hides
  closed rows from the open list, with a line saying how many closed since the snapshot.
  "Data as of" stamps next to the counts.
- **Machine translation.** Quotes from `text_en` carry "machine translation by the
  Commission, original in XX"; the about page explains it. 1,874 analyses read a
  translation.
- **Privacy.** Unregistered submitters with company size MICRO show as "Micro company,
  <country>"; digit runs of 8 or more in organisation names are masked
  (`privacy.display_submitter`). Applied to the landscape and the inbox.
- **Model non-answers.** A "what is asked" line that talks about the model's input or says
  nothing is asked is dropped (16 summaries had one); so is a key point that talks about
  "the provided text". The summary of 25931 ("Consesses") was regenerated through agy.
- **Own voice** reads "N submissions in this snapshot (81 consultations)" and says earlier
  portal submissions are not searched.
- **One source for the numbers.** `inspraak eval-report --write docs/eval.json` writes the
  table counts and keeps a hand-entered block (precision, recall, hubs). The about page
  reads that file; README and docs/EVAL.md were reconciled to it (held back 776, not 812;
  quotes 12,585 of 12,797; critic 38 of 49 on the 5 current drafts; reader cache 375 of
  378), and a test fails when they disagree. The per-provider call table moved into a
  collapsed block with plain provider names.

### Run

- Model calls: 1 `reader_summary` through agy (gemini-3.8-flash-low) for 25931, ok. No
  Gemini API calls, no claude_cli calls.
- Export: 166 files, 40 organisation pages, 81 consultation pages, 40 run pages. In the
  export: 0 pages with `chunk:`, "machine draft", "reviewer:", "decided by", "demo
  decision", "confidence 0/1", "provided text" or "Consesses"; 0 long dashes; every HTML
  page has the credit; all 40 organisation pages have noindex, no other page has it; 464
  "Micro company" rows; 1,271 translation labels.
- Sub-path check: the site copied into `inspraak/` and served from its parent, browsed at
  `/inspraak/` with Playwright (Chromium) at 390 and 1280 px: index, about, the T&E, EDRi,
  BEUC and ETF pages, consultation 25981 and the T&E run page. 0 console errors, 0
  responses of 400 or more, no horizontal scroll at either width, search works, countdowns
  filled ("53 days left" for 25981 on 5 October, Brussels time).
- The new critic rule on the 5 current drafts (not re-run, sentences counted with
  `is_normative`): 7 of the 38 kept sentences are positions in the organisation's voice,
  4 in BEUC's draft, 1 in T&E's, 2 in EDRi's (the age verification sentence among them).
  The stored critic output does not say which source each of them cited, so whether they
  would survive is not known; none of them is on the public site.

### Verified

- `make test`: 119 passed (was 111). `make lint`: clean.
- The api container restarted healthy; `/`, `/about`, `/inbox`, an organisation page and
  consultation 25931 return 200 and carry the footer credit.

### Unfinished

- The snapshot is from 3 October. Deadlines and counts on the portal have moved since (the
  review found 25981 extended to 30 November). Re-ingest the open publications and
  re-export right before publishing.
- The drafts in the database were checked by the old critic. The 7 position sentences
  above would need a re-run of drafter and critic to apply the new rule.
- The run pages of the 5 organisations with drafts show the brief stage of their 3 October
  run, so their match table is the v1 list (with a line saying the page recomputes it).
- The page still shows the matcher's relevance and similarity scores; only the stance
  confidence was removed.
- No Open Graph image and no `og:url`: there is no public URL yet.

## Data refresh before publishing, 6 October 2026

The snapshot of 3 October was refreshed so the public pages are true on the day they go out.

### Code

- `inspraak/ingest/hys.py`: run times use the Brussels clock (`portal_now`) instead of the
  machine clock. New `refresh_closed_since_fetch`: an in-scope publication whose feedback
  period ended after its last fetch gets one more fetch past the page cache, and its scope
  reason says it closed. It runs at the end of every `ingest hys`, with or without
  `--skip-closed`. One test in `tests/test_hys.py`.
- `scripts/outreach_check.py`: reads deadlines, counts by submitter type and organisation
  rows live from the backend for a list of organisations and their top consultation, and
  whether the organisation already answered any stage of the same initiative. Citizen rows
  are counted, never named.

### Run (5 October, Brussels time)

- `inspraak ingest hys --skip-closed` with the 20 hour page cache: 138 requests, all cache
  misses, finished 23:30. No new consultation in the 90 day window; scope stays 81
  publications, 41 open. Submissions on the 41 open publications: 1,038 to 2,503 (643 to
  681 from organisations). Child Safety Online (26173) went from 84 to 1,507; the public
  procurement deadline (25981) moved from 27 to 30 November. Schengen visas (25894, closes
  6 October) and the meat freezing rule (25881, closed 5 October) were still open at the
  time of the fetch; the pages close them by date in the reader's browser.
- Documents: 11 new submission attachments parsed (52 pages, 189 chunks); no new
  Commission documents. Meetings file of 5 October: 11,531 meetings (was 11,527). Embed:
  26 submissions, 189 chunks, 37 meeting subjects.
- Backfill (`scripts/backfill.py agy,claude_cli`, `AGY_MODELS=gemini-3.8-flash-low`): 38
  new organisation rows, 26 analysed in 3 batched agy calls, 12 without readable text, 0
  errors, 0 left. No claude_cli calls, no Gemini API calls.
- Reloading the meetings file empties `publication_meetings` through the foreign key
  cascade; the backfill's summary pass rebuilt it (4,825 rows, 81 publications).
- `inspraak demo --replace`: 35 briefs rebuilt, 0 model calls (every rerank pair was
  cached). The 5 organisations with drafts got a new brief run each (0 calls), so their
  public run page now shows the current list; their draft runs are kept in the local app.
  That adds 5 open go / no-go items to the inbox.
- `eval-report --write docs/eval.json`: analysis rows 9,155 (6,531 analysed, 2,624 without
  text), held back 778, quotes 12,633 of 12,845 (98.3%), translations 1,879, runs 49
  current and 70 superseded, reader cache 560 of 563. README and docs/EVAL.md reconciled
  (new section "Refresh of 5 October").
- `make export`: 166 files, 40 organisation, 81 consultation and 40 run pages. Banner
  "Static snapshot of 5 Oct 2026", "Data as of 5 Oct 2026, 23:30". No page shows the old
  procurement deadline; BEUC's page no longer lists the draft-only procurement card.
- Sub-path check as before (`/inspraak/`, Playwright Chromium, 390 and 1280 px; index,
  about, T&E, EDRi, BEUC, ETF, consultation 25981, T&E run page): 0 console errors, 0
  responses of 400 or more, no horizontal scroll, search works, 25981 counts down to
  30 November.
- Live check of the outreach facts for 10 Brussels organisations against the backend, 5
  October 23:38 to 23:40 Brussels time. Corporate Europe Observatory was left out: its top
  card (ETS review) does not fit its work.

### Verified

- `make test`: 120 passed. `make lint`: clean. The api container restarted healthy; `/`,
  `/about`, `/inbox`, the BEUC page and consultation 25981 return 200.
- No em dash, en dash or Unicode minus in the changed files or the export.

### Unfinished

- The register was not downloaded again (export of 2 October).
- 25894 and 25881 close within a day of the fetch. A final fetch for them happens on the
  next `ingest hys` after their end date; run it and export again on the day of publishing.
- The landscape counts on the pages are the Brussels evening of 5 October; the portal
  keeps moving (26173 had 1,507 rows at 23:30 and 1,512 at 23:39).
