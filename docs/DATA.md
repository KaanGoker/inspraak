# Data

Every source the snapshot is built from, what was fetched, the counts after the full V1 ingest
of 3 October 2026, and the traps met on the way. Everything below runs from
`inspraak ingest ...` and lands in the Postgres container (`docker compose`, host port 5544).
The demo never calls these endpoints at request time.

Each endpoint was re-verified with one live request on 3 October 2026 before the ingest ran;
the verified shape is noted per source.

## 1. EU Transparency Register

- URL: `https://transparency-register.europa.eu/odplastorganisationxml_en`
- Verified: HTTP 200, `application/octet-stream`, 116,763,563 bytes (HEAD). The file header
  says `exportDate 2026-10-02T20:00:00`, `numberOfIR 17897`.
- Command: `inspraak ingest register` (`--no-download` reuses `data/raw/register/register.xml`).
- Table: `organisations`, one row per registrant, `tr_id` is the register ID.

Counts (run of 3 October 2026, export of 2 October 2026):

| What | Count |
| --- | --- |
| Registrants declared in `metaData/numberOfIR` | 17,897 |
| Rows stored | 17,897 |
| Belgium head office | 2,786 |
| Companies and groups | 5,350 |
| NGOs, platforms and networks | 4,888 |
| Trade and business associations | 3,011 |
| Trade unions and professional associations | 1,146 |
| Professional consultancies | 817 |
| Think tanks and research institutions | 780 |
| Profile vectors | 17,897 |

Traps:

- The file declares XML 1.1 and contains a few character references to control characters
  (`&#x2;`, `&#xb;`, `&#x1d;`). libxml2 rejects them. Parsing with `recover=True` looks like a
  fix but silently drops `&amp;` in thousands of later records ("Companies  groups"). The
  loader rewrites the header to 1.0 and replaces the bad references by a space on a streaming
  byte filter, then parses strictly.
- A cut download still parses. The parsed count is checked against `numberOfIR` and a
  mismatch aborts the load. The research note of 2 October had a 51 MB partial copy with
  11,415 records for exactly this reason.
- The profile text that is embedded is name, acronym, category, interests, goals and the EU
  legislative proposals field. Financial data is kept as JSON in `financial`.

## 2. Have Your Say (European Commission, Better Regulation portal)

The portal's own JSON backend, undocumented, no terms, no stability promise. Base
`https://ec.europa.eu/info/law/better-regulation`. One request per second, every response
cached under `data/raw/hys/` before it is parsed, so an interrupted run resumes without
repeating requests.

### 2a. Initiative listing

- `GET brpapi/searchInitiatives?size=100&page=N&language=EN`
- Verified: HTTP 200 JSON, `initiativeResultDtoPage.totalElements` 4128, first row id 19418
  "Child Safety Online".
- Trap: `receivingFeedbackStatus=OPEN` is ignored by the server. OPEN, CLOSED, UPCOMING and no
  filter all return the same 4,128 rows. Open consultations are selected client-side from
  `currentStatuses[].receivingFeedbackStatus` and `feedbackEndDate`.
- Trap: `id` arrives as a float (`19418.0`).
- Trap: the 4,128 listing rows hold 4,109 distinct ids; 19 initiatives appear twice across
  pages. The upsert keeps one row each.
- Cache: `data/raw/hys/listing/page-NNNN.json`, 42 pages, reused for 20 hours.

Counts: 4,109 initiatives stored. By current feedback status in the listing: OPEN 34,
CLOSED 1,480, DISABLED 2,596, no status 18.

### 2b. Initiative detail

- `GET brpapi/groupInitiatives/{id}`
- Verified: HTTP 200 JSON, id 19418, `publications[0].id` 26173, `totalFeedback` 339, status
  OPEN, attachments with `documentId`, `filename`, `language`, `pages`, `title`.
- Only fetched for initiatives in the scope plan (90 of 4,109). Commission staff contact
  fields on a publication (`authorName`, `authorMail`, ...) are dropped before caching.
- Trap: a grouped call for evidence is one publication under several initiatives
  (publication 25641 under 18872, 18873 and 18874). `publication_initiatives` keeps every
  parent; `publications.initiative_id` is the lowest parent id; the feedback is fetched once.
- Cache: `data/raw/hys/initiatives/{id}.json`. Details of closed initiatives are kept forever,
  open ones for 20 hours.
- Refresh: `inspraak ingest hys --skip-closed` re-reads the listing and every open
  publication once its pages are older than 20 hours. An in-scope publication whose
  feedback period ended after its last fetch gets one more fetch that skips the cache
  (`refresh_closed_since_fetch`), so its final count and deadline are true; it stays in
  scope and the pages treat it as closed by its end date. Run times use the Brussels clock.

### 2c. Scope (V1)

From `SPEC.md`: publications whose feedback period is open and closes within 90 days, plus up
to 40 recently closed publications (last 365 days, newest first, 250 initiative details
scanned) with at least 20 organisation submissions.

| Scope | Publications | Organisation rows | Citizen rows |
| --- | --- | --- | --- |
| Open, closing by 1 January 2027 | 41 | 643 | 395 |
| Closed, 20 or more organisations | 40 | 8,474 | 21,514 |
| Total in scope | 81 | 9,117 | 21,909 |

Publication types in scope: OPC_LAUNCHED 16, CALL_FOR_EVIDENCE 15, PROP_REG 12,
REG_DEL_DRAFT 9, CFE_IMPACT_ASSESS 9, REG_IMPL_DRAFT 6, CFE_EVL_FC 5, DEC_IMPL_DRAFT 3,
COMMUNIC 2, PROP_DIR 2, REG_DRAFT 1, DIR_DRAFT 1.

The `feedback` table holds 31,067 rows in total (a few more than the scope sums, from a
publication whose rows were fetched before it was dropped from the kept set). Of the
organisation rows, 2,424 carry a register ID and 2,353 of those join the register export.

### 2d. Feedback

- `GET api/allFeedback?publicationId=..&size=100&page=N&sort=dateFeedback,ASC`
- Verified: HTTP 200 JSON, plain Spring page (`content`, `totalPages`, `totalElements`).
  Publication 25704: 7,133 rows. Raw row keys seen today: attachments, country, dateFeedback,
  feedback, feedbackTextUserLanguage, firstName, id, isDislikedByMe, isLikedByMe,
  isMyFeedback, language, login, publication, publicationId, publicationStatus,
  referenceInitiative, status, surname, userType.
- Trap: the path is `/api/`, not `/brpapi/`. The `/brpapi/allFeedback` variant answers 200
  with the HTML shell of the site; the fetcher refuses any non-JSON content type.
- Trap: `feedbackTextUserLanguage` is, despite its name, the English translation of a
  non-English submission. Stored as `feedback_text_en` (1,883 rows have one).
- Trap: popular files run to tens of thousands of rows (80,159 on the tobacco impact
  assessment). The fetch stops at 5,000 rows per publication and sets `feedback_truncated`.
  Five closed publications are truncated: 23091, 23559, 23697, 25571, 25704. Sorting by date
  ascending means the truncated set is the earliest 5,000.
- Privacy guard (`inspraak/privacy.py`), applied before the page is written to disk and
  again before the insert:
  - `firstName`, `surname` and `login` are dropped from every row, organisation rows too.
  - Rows with `userType` EU_CITIZEN or NON_EU_CITIZEN keep only id, type, country, language,
    date and the attachment count. Text, translation, organisation field, register number
    and attachment metadata are removed.
  - Fail closed: an unknown `userType`, or an organisation type with an empty organisation
    field, is treated as a citizen row.
  - The `feedback` table has no name columns and a CHECK constraint that refuses content on a
    citizen row. `tests/test_privacy.py` and `tests/test_db.py` cover both layers.
- Cache: `data/raw/hys/feedback/{publicationId}/page-NNNN.json`, scrubbed, 28 MB.

Feedback rows by stakeholder type (whole table): EU_CITIZEN 20,684; COMPANY 4,243;
BUSINESS_ASSOCIATION 1,529; NON_EU_CITIZEN 1,239; NGO 981; OTHER 796;
ACADEMIC_RESEARCH_INSTITTUTION 688 (the Commission's own spelling, kept as is);
PUBLIC_AUTHORITY 502; ENVIRONMENTAL_ORGANISATION 209; TRADE_UNION 156;
CONSUMER_ORGANISATION 40.

Of 9,143 organisation rows, 3,015 have at least one attachment and 3,237 have no text in
the row at all (attachment only).

### 2e. Documents

- `GET api/download/{documentId}`
- Verified on 2 October: HTTP 200, `application/pdf`, file starts with `%PDF-`.
- Two kinds in the `documents` table: `proposal` (the Commission's own documents of a
  publication) and `feedback_attachment` (files organisations attached to their feedback).
- Trap: the endpoint mostly serves a PDF rendition whatever the original name says (a
  `.docx` submission comes back as PDF). The type is sniffed from the bytes, never from the
  name. A zip that is not a Word document (the Commission publishes zip exports of replies)
  is not parsed.
- Trap: a wrong id answers 200 with the HTML shell; refused by content type and magic bytes.
- Proposal selection: per attachment category, English where it exists, else the first of
  FR, DE, NL, else the first file. 145 proposal documents registered for 81 publications
  (144 EN, 1 FR). 14 in-scope publications have no Commission document at all (typical for
  a call for evidence or a draft delegated act with the text inline).
- Text per page with PyMuPDF (`document_pages`), then chunks of at most 900 characters that
  never cross a page (`chunks`), so a citation is always one page number. Fragments under
  40 characters (page numbers, lone headers) are dropped unless they are the only content.
- Files over 40 MB are skipped and marked `error`.
- Cache: Commission documents under `data/raw/hys/documents/{documentId}.bin` (116 MB, kept
  forever because a documentId is immutable). Attachments are not kept on disk unless
  `KEEP_ATTACHMENT_FILES=1`; their text lives in Postgres and `parse_status` makes the run
  resumable.

| Documents | Count |
| --- | --- |
| Proposal documents registered | 145 |
| Proposal parsed ok | 143 |
| Proposal errors | 2 (publication 20209: a 1.3 MB zip of contributions, not a document; a 76 MB zip over the size limit) |
| Proposal pages | 6,889 |
| Proposal chunks | 27,025 |
| Feedback attachments registered | 3,271 |
| Feedback attachments parsed ok | 3,257 (14 still pending, see BUILD_LOG stage 2) |
| Feedback attachments empty (no text layer) | 0 |
| Feedback attachments error | 0 |
| Attachment pages | 29,622 |
| Attachment chunks | 112,819 |

## 3. Commission meetings with interest representatives

- URL: `https://ec.europa.eu/transparency-initiative/meetings/data/meetings/dataxlsx?name=meetingscommissionrepresentatives2429`
- Verified: HTTP 200, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`.
  The first copy (2 October) was 922,197 bytes; the copy loaded on 3 October (file generated
  2026-10-03) is the one the counts below come from.
- Command: `inspraak ingest meetings`. The file is regenerated in full, so the table is
  replaced in one transaction; the row id is a hash of the row content, stable across loads,
  and vectors of unchanged subjects are kept.

| What | Count |
| --- | --- |
| Meetings (rows) | 11,527 |
| Date range | 2024-11-21 to 2026-10-02 |
| Meeting to organisation links | 16,413 |
| Links whose register ID is in the register export | 16,007 |
| Links whose register ID is not in the export (left the register) | 406 |
| Rows with more than one organisation | 1,515 |
| Subject vectors | 11,527 |

Traps:

- Row 1 is the generation date, the header is row 2.
- Two rows can be identical in every column (same cabinet, date, subject and organisations).
  The row id is a hash of the content, so identical rows get a `-2`, `-3` suffix instead of
  collapsing into one meeting. Without it an organisation's meeting count came out too low
  (checked against the live file: Eurocities 21, BusinessEurope 83, FoodDrinkEurope 22,
  EuroCommerce 34).
- Several organisations in one row: names and register IDs are comma separated in two cells,
  and names contain commas themselves ("Atlantic Council of the United States, Inc"). Only
  the IDs are split (pattern `digits-digits`); a name is attached to an ID only when both
  lists have the same length, otherwise the name comes from the register join.
- Subjects are short free text ("Eu environmental priorities"). Linking a meeting to a
  consultation is by embedding similarity and is shown as approximate.

## 4. Vectors

Local model, no API quota: `paraphrase-multilingual-MiniLM-L12-v2` through fastembed (ONNX,
CPU), 384 dimensions, cosine distance, HNSW indexes. Model files under `data/models/`.

Trap: the model reads 128 tokens and silently drops the rest (two texts that differ only
after token 128 give identical vectors). Texts longer than about 480 characters are cut into
windows of 480 characters (at most 6), each window is embedded and the normalised mean is
stored.

| Target | Source text | Rows with a vector |
| --- | --- | --- |
| `organisations.profile_embedding` | name, acronym, category, interests, goals, EU proposals | 17,897 |
| `publications.embedding` | initiative title(s), publication title, dossier summary, topics | 299 |
| `feedback.embedding` | organisation rows only, English translation preferred | 5,906 |
| `chunks.embedding` | proposal and attachment chunks | 139,844 (all) |
| `meetings.embedding` | the raw subject | 11,527 |

Throughput on the MSI laptop (CPU): about 50 chunks per second.

Matching baseline (`inspraak eval-match`, no LLM): for the 1,526 registrants that filed
feedback on an in-scope publication (2,259 organisation to publication pairs), the in-scope
publications are ranked by cosine similarity to the profile vector and the rank of the
publication they really answered is scored. Recall at 5: 0.425, recall at 10: 0.556, MRR
0.301, median rank 8 of 81. Random order: 0.067, 0.122, 0.067, median 39. This is a recall
measure only; it exists so a matcher change can be compared to a number.

## 5. Not fetched

- FTS 2024 "EU funding on record" and EP procedure steps: optional in the spec, skipped in
  this stage.
- Integrity Watch EU meetings JSON and the EP open data API: listed in the research, not used.
- Meetings of the 2014 to 2019 and 2019 to 2024 mandates.

## 6. Raw cache on disk

`data/` is gitignored. After the full ingest: `data/raw/register/register.xml` 117 MB,
`data/raw/meetings/meetings2429.xlsx` 922 KB, `data/raw/hys/` about 162 MB (listing 14 MB,
initiatives 4.5 MB, feedback 28 MB scrubbed, documents 116 MB). Logs of the long runs are
under `data/logs/`.

Every load is recorded in `ingest_runs` with its stats (`inspraak db runs`).
