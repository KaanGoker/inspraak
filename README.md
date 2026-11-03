<p align="center"><img src="assets/brand/cover/cover-2x1.png" alt="Inspraak" width="100%"></p>

# Inspraak

[![CI](https://github.com/KaanGoker/inspraak/actions/workflows/ci.yml/badge.svg)](https://github.com/KaanGoker/inspraak/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![Licence MIT](https://img.shields.io/badge/licence-MIT-green)](LICENSE)

Inspraak finds the open European Commission consultations a Brussels organisation should answer, shows who already responded and what they asked, and drafts a reply only after a person says go. Built with LangGraph (two human checkpoints and a critic that removes any sentence it cannot cite), Postgres with pgvector and the Gemini free tier.

**Live demo:** [kaangoker.dev/inspraak](https://kaangoker.dev/inspraak/), a static snapshot of 5 October 2026 with 40 Brussels organisations and 81 consultations. No drafts and no review decisions are public.

Built by [Kaan Goker](https://kaangoker.dev) ([LinkedIn](https://linkedin.com/in/kaangoker)). Not affiliated with any organisation it shows.

![Organisation page](assets/organisation.png)

## What you get

Type the name or the Transparency Register ID of an organisation and get one page:

- **The open consultations that fit it.** One card per initiative, each with a one-line reason why it fits, sorted by relevance.
- **Who already answered.** Submissions counted by stakeholder type and country, and for each organisation submission the stance the model reads and up to three key asks with a verbatim quote. Every quote is checked word for word against the source and dropped when it is not found.
- **What the proposal says.** The passages of the Commission document closest to the organisation's profile, each cited to a page of the PDF.
- **Who met the cabinets** on a similar subject, from the Commission's own meeting lists. The match is by subject text and is marked approximate.
- **A draft response** of at most 4,000 characters, the Have Your Say limit. It is written only after a reviewer says go, checked sentence by sentence by a critic, and waits for a second human decision. Nothing is ever submitted by the system.

Every number and quote links to its public source. The public demo shows the first four parts; drafts and review decisions stay in the local app.

## How it works

Two layers sit on one Postgres snapshot of three public sources: the EU Transparency Register (17,897 registrants), the Commission's Have Your Say portal (81 consultations in scope, 41 of them open) and the Commission's list of meetings with interest representatives (11,531 meetings).

The **publication layer** runs once per consultation and is shared by every organisation: a summary of the Commission document, one analysis per organisation submission with its quotes verified, and a meetings map that uses no model. All of it is cached forever, so a second organisation on the same consultation costs no model call.

The **organisation layer** is one LangGraph run per organisation. A supervisor routes between the agents on what the state already holds. The run stops twice for a person, at go or no-go and at draft review, and waits in a Postgres checkpointer, for days if needed.

```mermaid
graph TD
    START([start]) --> supervisor
    supervisor -.-> profiler
    supervisor -.-> matcher
    supervisor -.-> landscape
    supervisor -.-> lobby
    supervisor -.-> reader
    supervisor -.-> go_no_go
    supervisor -.-> drafter
    supervisor -.-> critic
    supervisor -.-> review
    supervisor -.-> memory
    profiler --> supervisor
    matcher --> supervisor
    landscape --> supervisor
    lobby --> supervisor
    reader --> supervisor
    go_no_go --> supervisor
    drafter --> supervisor
    critic --> supervisor
    review --> supervisor
    memory --> END([end])
```

| Agent | Model calls | What it does |
| --- | --- | --- |
| profiler | 0 | The register entry, reviewer corrections, past submissions as the organisation's voice |
| matcher | 0 or 1 | Embedding similarity with a hub penalty, one card per initiative, then one batched rerank that returns a relevance and a reason per consultation, cached |
| landscape | 0 | Counts, stances and verified quotes from the cache |
| lobby | 0 | Cabinet meetings on a similar subject |
| reader | 0 or 1 | The cached summary and the passages closest to the profile, with page numbers |
| go / no-go | 0 | Human checkpoint: the run pauses until a person decides per consultation |
| drafter | 1 per go | A draft in the organisation's voice, within 4,000 characters |
| critic | 1 per draft | A fact needs a Commission passage, a position needs the organisation's own past submission or register goals, anything else is removed and listed |
| review | 0 | Human checkpoint: approve, edit or reject; an edit is kept as a preference for the next draft |

A cold organisation with one go costs 3 model calls. Reading a page costs none. The live path uses `gemini-3.5-flash-lite` on the free tier behind a shared rate limit; `LLM_PROVIDER=fake` is a deterministic stand-in that runs the whole system and the full test suite without a key. Details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Measured

All numbers come from one file, [docs/eval.json](docs/eval.json), which the about page reads at render time; a test fails when this README or [docs/EVAL.md](docs/EVAL.md) quotes a different number. Snapshot of 5 October 2026.

- **Matching precision.** On 30 random organisation and consultation pairs, 15 of 30 suggested consultations were relevant (50%) and 6 of 9 first places were right, judged by a blind language-model judge against a written rubric (inputs and labels in [docs/eval/](docs/eval/)). The list is a shortlist to check, not a ranking to trust.
- **Recall against real behaviour.** For 1,526 registrants that really answered an in-scope consultation, the one they answered is in their top 5 in 42.5% of cases by embedding alone (random order: 6.7%). On a sample of 150, the hub penalty and the rerank raise that to 65.1% and move the median rank from 6 to 2.
- **Quotes.** 12,633 of 12,845 key-ask quotes were found verbatim in their own submission (98.3%). The other 212 were dropped before storage.
- **Submissions.** 9,155 organisation submissions: 6,531 analysed, 2,624 without readable text and counted only. 778 analyses with a low confidence or an unclear stance are held back from the pages.
- **Critic.** Across the 5 current drafts, 38 of 49 sentences were supported by a retrieved passage or the organisation's own material (77.6%); the 11 others were removed and listed on the draft.
- **Cost.** The whole open scope cost 195 model calls. After that, the brief for 40 organisations cost one cached rerank call each, and each draft costs 2. Reader cache: 560 of 563 summary reads.

## Limits, stated plainly

- Everything runs from a local snapshot. Deadlines are dates; the countdown is computed in the reader's browser, so a page read weeks later still says "closed" at the right time.
- Stances, key asks, summaries and match reasons are model output. They are labelled as such on the pages, and the stance tag reads "model reads as".
- Matching is similarity plus one model judgment. Half of the suggested consultations in the sample above were relevant; broad profiles (think tanks, consultancies) get a mixed list.
- The meetings link is by subject text only. Generic subjects such as "exchange of views" also pass.
- Submissions that arrive as a questionnaire answer or a scanned attachment are counted, not analysed.
- Citizens are counted and never stored: no name, text or register number of a citizen row reaches the database, and a constraint refuses one. Unregistered micro companies are shown by size and country, because such a name is often a person's name.
- The Have Your Say endpoints are the portal's own backend, not a documented API.

## Run it locally

You need Docker, Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env              # LLM_PROVIDER=fake works without a key
make demo-local                   # containers, a small fixture snapshot, the app on http://127.0.0.1:8010
```

`make demo-local` loads the same small snapshot the tests use (three registrants, one open consultation, a two-page document, four meetings), runs the publication layer on the deterministic provider and starts one run that waits in the review inbox, so every page and the human checkpoint can be tried in a few minutes. `make up` and `make web` run the containers and the app on their own.

A full snapshot (`make ingest`) downloads about 280 MB from the Commission at one request per second and takes a few hours; `make precompute` and `make demo` then build the publication layer and the 40 demo pages within the daily model budget. `make export` writes the public static site under `site/`. `make test` runs the 120 tests, each run on its own test database; `make lint` runs ruff.

A run from the command line:

```bash
uv run inspraak org search "BEUC"
uv run inspraak run start 9505781573-45        # stops at go or no-go
uv run inspraak review list
uv run inspraak review decide 10 '{"go": [25981]}'
uv run inspraak run show <run_id>              # trace, drafts, call counts
```

Set `GEMINI_API_KEY` and `LLM_PROVIDER=gemini` for real model output. Before the app is reachable by anyone else, set `REVIEW_TOKEN` (it then guards every page) and a real `POSTGRES_PASSWORD`.

## Data sources and reuse

- [EU Transparency Register](https://transparency-register.europa.eu/odplastorganisationxml_en), full XML export.
- [Have Your Say](https://ec.europa.eu/info/law/better-regulation/have-your-say_en): initiatives, feedback and Commission documents, through the portal's JSON backend.
- [Commission meetings with interest representatives](https://ec.europa.eu/transparency-initiative/meetings/listcommissioners?collegeid=0), mandate 2024 to 2029.

Commission documents and data are reused with attribution under the Commission's reuse policy. The feedback backend publishes no terms, so it was read at one request per second, once, into a local snapshot; no page ever calls it. Sources, counts and traps: [docs/DATA.md](docs/DATA.md).

## More

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (graph, interrupts, tables, providers, export), [docs/DATA.md](docs/DATA.md) (sources and their traps), [docs/EVAL.md](docs/EVAL.md) (every number and how it was measured). Licence: MIT, see [LICENSE](LICENSE). Copyright 2026 Kaan Goker.
