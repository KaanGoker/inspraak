# Evaluation

Measured on 3 October 2026 against the local snapshot (17,897 registrants, 81 in-scope
publications, 41 of them open), model `gemini-3.5-flash-lite` on the free tier, budget
`LLM_RPM=10`, `LLM_RPD=300` (self-imposed, shared through Redis). Every number below was read
back from the tables with `uv run inspraak eval-report` or `uv run inspraak eval-match`,
or counted by hand where it says so. The raw outputs are kept under `data/logs/` (not in git).

## Headline numbers (one source)

Every number the about page, the README and this file quote comes from `docs/eval.json`.
`uv run inspraak eval-report --write docs/eval.json` rewrites its `measured` block from the
tables and keeps its `manual` block (the hand check and the recall runs below, entered by
hand with their dates). The about page reads the file at render time, and
`tests/test_web.py::test_about_numbers_come_from_the_eval_file` fails when the README or
this file quotes a different critic, held back, quote or precision number. Definitions:
analyses count every provider except the test stand-in; "held back" are analysed rows with
a low confidence or an unclear stance; the critic counts version 1 of every draft a reviewer
did not reject; runs count those not superseded.

Written on 6 October 2026 from the snapshot refreshed on 5 October, 23:30 Brussels time (Have Your Say and meetings; see "Refresh of 5 October" at the end):

| What | Number |
| --- | --- |
| Organisation submissions analysed | 6,531 of 9,155 (2,624 without readable text) |
| Held back for review | 778 |
| Quotes verified verbatim | 12,633 of 12,845 (98.3%) |
| Analyses read from the Commission's English machine translation | 1,879 |
| Critic, current drafts | 38 of 49 sentences kept (77.6%) across 5 drafts |
| Runs | 49 current for 40 organisations, 70 superseded |
| Reader cache | 560 of 563 summary reads |
| Match precision (hand check, 5 October) | 20 of 30 |

The 3 October text below reported 812 held back analyses; the tables gave 776 on 6
October before the refresh and 778 after it, and 778 is the number used everywhere now.

## Runs

What was actually run to build the snapshot and the pages, in order. Every count was read
back from the tables or the command output at the time.

- **3 October, ingest.** Register export of 2 October: 17,897 registrants stored, 2,786
  with a Belgian head office. Have Your Say: 4,109 initiatives from 42 listing pages, 90
  details, 81 publications in scope (41 open, 40 closed with 20 or more organisation
  submissions), 31,067 feedback rows, 489 requests at one per second. Documents: 145
  Commission documents (143 parsed, 6,889 pages, 27,025 chunks) and 3,271 submission
  attachments (3,257 parsed, 29,622 pages, 112,819 chunks). Vectors for all 139,844 chunks
  on a laptop CPU at about 50 chunks a second.
- **3 October, Gemini API.** Open scope precomputed in 195 calls (41 summaries, 154
  analyses); the pass over the whole scope stopped cleanly at 45 of 81 publications when
  the daily budget of 300 calls was spent. `inspraak demo`: 38 runs up to go / no-go in
  22 seconds and 0 calls. Five go decisions, 2 calls each (drafter, critic).
- **3 October, batch backfill.** The rest of the publication layer through the batch
  providers: Claude Haiku analysed 1,328 submissions in 17 minutes, Gemini Flash 4,897 in
  34 minutes (503 batched calls of 10) plus 30 single calls and the 35 missing summaries.
  0 failed calls; 81 of 81 publications summarised, 0 submissions left.
- **5 October, matcher v2.** Hub penalty, one card per initiative, rerank: 40 rerank
  calls for the demo set, 150 more for the recall sample, 35 waiting runs rebuilt for 0
  calls (every judgment and summary cached).
- **5 October, refresh and export.** Have Your Say and the meetings file fetched again
  (138 requests): 38 new organisation rows, 26 analysed in 3 batched calls; 11,531
  meetings. Export: 166 files, 40 organisation, 81 consultation and 40 run pages.

Provenance of the model output on the pages: feedback analyses by Gemini Flash Lite
(API, 240), Claude Haiku (batch, 1,328) and Gemini Flash (batch, 4,963); publication
summaries by Gemini Flash Lite (46) and Gemini Flash (35); match judgments by Gemini
Flash. The test suite runs on a deterministic stand-in and never calls a model.

## Demo set

`demo_set.csv`: 40 registrants with their head office in Belgium, all in Brussels except
the Flemish and Belgian national ones, spread over five groups:

| Group | Count | Examples |
| --- | --- | --- |
| NGOs and networks | 16 | BEUC, EEB, Transport and Environment, EDRi, WWF EPO, Bond Beter Leefmilieu, 11.11.11 |
| Think tanks | 6 | CEPS, EPC, Egmont, Bruegel, IEEP, ECIPE |
| Trade associations | 9 | BusinessEurope, Cefic, Eurochambres, EuroCommerce, ACEA, DIGITALEUROPE, Agoria, VBO FEB, FoodDrinkEurope |
| Trade unions | 5 | ETUC, industriAll Europe, ACV-CSC, FGTB-ABVV, ETF |
| Consultancies | 4 | Rud Pedersen, SEC Newgate EU, Acumen, logos |

Every one of them got at least five open consultations above the 0.45 floor, so each has
a page with matches. `make demo` (`inspraak demo`) starts one run per organisation without
one and stops each at the go / no-go interrupt.

Result: 40 runs (38 new in this stage), 0 errors. 35 wait at go / no-go, 4 wait at draft
review with a machine draft, 1 is done (BEUC, approved in stage 2). On 5 October the 35
runs waiting at go / no-go were rebuilt with matcher v2 (see "Matcher v2" below).

## Match precision, hand-checked sample (matcher v1, 3 October)

Sample: 30 organisation and consultation pairs drawn with `random.seed(42)` from the 190
pairs the 38 new runs produced (5 per organisation), so it covers ranks 1 to 5 and both
bands. Each pair was judged by reading the organisation's register entry (goals and
declared interests) next to the consultation's title and initiative.

Rule used: **relevant** when the consultation's subject sits inside the organisation's
stated mission, so that an organisation of that kind would plausibly answer it. A long
interest list alone (consultancies and federations tick almost every box) did not make a
technical implementing act relevant; a large horizontal file (public procurement, Cloud
and AI Development Act) counted for a general business federation or consultancy.

| # | Organisation | Rank | Consultation | Score | Band | Judged |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | AGE Platform Europe | 1 | 25397 Europol Regulation | 0.603 | match | no |
| 2 | AGE Platform Europe | 2 | 26059 data centre standards (consultation) | 0.581 | match | no |
| 3 | AGE Platform Europe | 3 | 26060 data centre standards (proposal) | 0.562 | match | no |
| 4 | AGE Platform Europe | 5 | 25731 victims' rights strategy | 0.544 | uncertain | yes (elder abuse) |
| 5 | Acumen Public Affairs | 1 | 26086 plant pest protective measures | 0.629 | match | no |
| 6 | Bond Beter Leefmilieu | 3 | 25761 ETS review | 0.575 | match | yes |
| 7 | Cefic | 3 | 25923 omnibus on taxation | 0.639 | match | yes |
| 8 | Cefic | 4 | 25981 public procurement | 0.618 | match | no |
| 9 | CEPS | 1 | 25397 Europol Regulation | 0.611 | match | yes (justice and home affairs unit) |
| 10 | CEPS | 2 | 25280 Horizon Europe joint undertakings | 0.587 | match | yes |
| 11 | DIGITALEUROPE | 5 | 25981 public procurement | 0.511 | uncertain | yes |
| 12 | Eurochambres | 5 | 25527 Dual-Use Regulation evaluation | 0.644 | match | yes |
| 13 | Eurochild | 1 | 25402 Eurojust Regulation | 0.597 | match | no |
| 14 | ETF | 2 | 26170 training of fishers | 0.563 | match | yes |
| 15 | FoodDrinkEurope | 4 | 25981 public procurement | 0.533 | uncertain | no |
| 16 | Friends of the Earth Europe | 2 | 26086 plant pest protective measures | 0.669 | match | no |
| 17 | Friends of the Earth Europe | 4 | 25527 Dual-Use Regulation evaluation | 0.660 | match | no |
| 18 | FGTB-ABVV | 4 | 25981 public procurement | 0.507 | uncertain | yes (social clauses) |
| 19 | Rud Pedersen Brussels | 4 | 25397 Europol Regulation | 0.627 | match | no |
| 20 | Transparency International EU | 1 | 25397 Europol Regulation | 0.747 | match | no (borderline) |
| 21 | Transport and Environment | 2 | 25794 ETS directive | 0.656 | match | yes |
| 22 | Transport and Environment | 3 | 25760 ETS, maritime and aviation | 0.640 | match | yes |
| 23 | Transport and Environment | 4 | 25761 ETS review | 0.636 | match | yes |
| 24 | VBO FEB | 5 | 26119 "Diversify EU" instrument | 0.472 | uncertain | yes |
| 25 | WWF European Policy Programme | 3 | 25761 ETS review | 0.666 | match | yes |
| 26 | WWF European Policy Programme | 4 | 25760 ETS, maritime and aviation | 0.661 | match | yes |
| 27 | industriAll Europe | 1 | 25280 Horizon Europe joint undertakings | 0.670 | match | no (borderline) |
| 28 | industriAll Europe | 2 | 26119 "Diversify EU" instrument | 0.655 | match | yes |
| 29 | industriAll Europe | 5 | 25821 Brussels Ia recast | 0.591 | match | no |
| 30 | logos public affairs | 3 | 24198 Cloud and AI Development Act | 0.615 | match | yes |

**Precision on the sample: 17 of 30 (57%).** By band: `match` 13 of 25 (52%),
`uncertain` 4 of 5. Counting the two borderline pairs as relevant gives 19 of 30 (63%).

What the sample shows:

- Specialists on their own file are right: every ETS pair for T&E, WWF and BBL, ETF on
  fishers' training, ECRE's top match (Schengen visas, not in the sample) and EDRi's (child
  safety online).
- One file acts as a hub. 25397 (Europol Regulation) appears in 21 of the 190 lists and is
  the first match of 5 organisations; 25981 (public procurement) appears in 20 and 25527
  (dual use) in 18. Long "Proposal for a REGULATION ..." texts sit close to every generic
  profile in the 384 dimension space. 26 of the 41 open publications appear at all.
- The score band does not separate good from bad matches: the cosine scores of relevant
  pairs (0.47 to 0.67) and irrelevant ones (0.53 to 0.75) overlap; the highest score in
  the sample (Transparency International on Europol) is a miss. The uncertain band is a
  review prompt, not a filter.
- Generic profiles (consultancies, think tanks with every interest ticked) match everything
  a little. The "not our topic" correction is there for that; it was not exercised in this
  sample because nobody reviewed these 35 runs.

Recall against what organisations really did (`inspraak eval-match`, no hand work):
1,526 registrants that filed feedback on an in-scope publication, 2,259 pairs. Recall at
5: 0.425 (random: 0.067), recall at 10: 0.556 (random: 0.122), MRR 0.301, median rank 8 of
81.

## Matcher v2 (5 October 2026)

The review before launch found two faults on the organisation pages: one file (Europol,
25397) led lists it did not fit, and publications of one initiative (the three ETS
proposals, the two data centre and the two Brussels Ia publications) took 2 or 3 of the 5
slots. Three changes, in `inspraak/agents/matcher.py` and `inspraak/agents/rerank.py`:

1. **Hub penalty.** Each publication gets `hub_score`: its mean cosine to its 200 nearest
   registrants (of 17,897), centred on the mean over the 81 in-scope publications. The
   matcher subtracts 0.5 times it. Tuned on `inspraak eval-match` over a grid (k of 10,
   50, 100, 200, 300, 500, 1,000, all; weight 0.25 to 1.0), with mean-centring of the
   vectors and a penalty on how often a publication sits in registrants' top 5 tried too.
   Mean-centring cost recall (recall at 5 0.374 to 0.405); the frequency penalty cost
   recall at every weight; k = 200, weight 0.5 is the setting where recall does not drop
   and the most frequent file in the 40 demo lists appears least often.
2. **One card per initiative.** Publications of one initiative, or with the same short
   title, collapse into one card: the one with a draft, else the open one with the
   nearest deadline, with the best score of the group. The others are listed as siblings.
3. **Rerank.** The 15 strongest cards go to the model in one call per organisation, with
   the register profile (name, category, goals, declared interests) and each consultation's
   title and cached summary. The prompt carries the same relevance rule as the hand check
   below. The model returns a relevance in [0, 1] and a one-line reason, shown on the card.
   Under 0.3 the card is dropped; 0.6 and over is the match band. Judgments are cached per
   organisation, publication and profile text (`match_reranks`). Model: Gemini Flash
   through the batch provider, outside the Gemini API budget. The fake provider stands in
   for it in the tests.

### Recall against real submissions

`inspraak eval-match`, same ground truth as before (1,526 registrants, 2,259 pairs, 81
publications in 67 initiatives). Per publication is the number reported on 3 October; per
initiative counts a hit when the answered publication's initiative is in the list, which
is what a page with one card per initiative shows.

| Ranking | Recall at 5 | Recall at 10 | MRR | Median rank |
| --- | --- | --- | --- | --- |
| per publication, embedding (v1) | 0.425 | 0.556 | 0.301 | 8 |
| per publication, with hub penalty | 0.429 | 0.565 | 0.302 | 8 |
| per publication, random | 0.067 | 0.122 | 0.067 | 39 |
| per initiative, embedding | 0.453 | 0.590 | 0.343 | 7 |
| per initiative, with hub penalty | 0.472 | 0.602 | 0.347 | 7 |
| per initiative, random | 0.090 | 0.165 | 0.083 | 31 |

With the rerank, on a random sample of 150 of these registrants
(`inspraak eval-match --rerank-sample 150`, seed 42, 150 batch calls to Gemini Flash, 0 errors): the
15 strongest initiatives per organisation (open and closed, so every answered file can
appear) reordered by the model, recall counted per initiative on the same 150.

| Ranking (150 sampled registrants, per initiative) | Recall at 5 | Recall at 10 | MRR | Median rank |
| --- | --- | --- | --- | --- |
| embedding | 0.453 | 0.599 | 0.375 | 6 |
| with hub penalty | 0.486 | 0.608 | 0.380 | 6 |
| with hub penalty and rerank | **0.651** | **0.712** | **0.473** | **2** |

The rerank moves the consultation an organisation really answered from a median rank of 6
to 2. Recall at 10 is capped by the pool: a file outside the 15 candidates cannot be
brought up. One caveat: the model may know from its training data that a known
organisation answered a known consultation; the profiles in the sample are mostly small
registrants, where that is unlikely to carry much.

### Hub concentration in the demo lists

The 35 rebuilt runs (below), against the lists the same 35 runs held from 3 October:

| | 3 October (v1) | 5 October (v2) |
| --- | --- | --- |
| organisation and consultation pairs | 175 | 162 |
| distinct consultations | 24 | 31 |
| lists with two cards of one initiative | 16 of 35 | 0 of 35 |
| Europol (25397) | in 19 lists | in 4 lists (ECRE, EPC, Egmont, TI EU) |
| most frequent consultation | 25981 public procurement, 20 | 26119 "Diversify EU", 18 |

List lengths after the rerank: 27 lists of 5, 5 of 4, 2 of 3, 1 of 1 (AGE Platform: only
the victims' rights strategy is left). The pages show fewer cards where the model judged
the rest unrelated.

### Demo runs rebuilt

The organisation pages compute their list on read, so they use the new matcher at once.
The runs store the list they were started with, so they were rebuilt:
`inspraak rerank` (40 batch calls to Gemini Flash, 0 errors, 558 judgments cached), then
`inspraak demo --replace`. The 35 runs that still waited at an undecided go / no-go were
closed as `superseded` (with their open review items, nothing deleted) and 35 new runs
started; they cost 0 model calls, every rerank and summary came from the caches. The 5
runs with a draft (BEUC, Zero Waste Europe, T&E, EDRi, ETF) were kept: their drafts and
decisions belong to them. Their pages use the new list. A consultation with a draft stays
on its page even when the matcher no longer ranks it: BEUC's approved draft on public
procurement (25981) now shows "relevance 0.10" with a line saying it is shown because of
the draft.

### Match precision, hand-checked sample (v2)

Same method as on 3 October: 30 pairs drawn with `random.seed(42)` and `random.sample`
from the 162 pairs the 35 rebuilt runs produced, ordered by organisation and rank
(`scripts/precision_sample.py --since '2026-10-05 20:25:00+00'`, which prints each pair
with the register profile and the consultation summary). Same rule: relevant when the
consultation's subject sits inside the organisation's stated mission; a long interest
list alone does not make a technical act relevant; a large horizontal file counts for a
general business federation or a consultancy.

**Who judged.** Claude (the coding agent that built this stage), reading the register
goals and declared interests next to the consultation title and summary. Not Kaan, not a
domain expert, and not blind: the judge built the matcher and saw the model's relevance
next to each pair. To check that the judge is no softer than the 3 October one, the same
judge re-judged the 30 pairs of the v1 table above with the same rule: same verdict on all
30, 17 relevant.

| # | Organisation | Rank | Consultation | Relevance | Band | Judged |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | AGE Platform Europe | 1 | 25731 victims' rights strategy | 0.8 | match | yes |
| 2 | Acumen Public Affairs | 1 | 25981 public procurement | 0.8 | match | yes (horizontal, consultancy) |
| 3 | Acumen Public Affairs | 3 | 25759 energy and tyre labelling omnibus | 0.7 | match | no (borderline) |
| 4 | Cefic | 4 | 26119 "Diversify EU" instrument | 0.7 | match | yes |
| 5 | CEPS | 1 | 26119 "Diversify EU" instrument | 0.9 | match | yes |
| 6 | CAN Europe | 2 | 26060 data centre standards | 0.8 | match | yes (energy efficiency) |
| 7 | CAN Europe | 3 | 26084 air pollution programme format | 0.7 | match | no |
| 8 | ACV-CSC | 4 | 23919 territorial supply constraints | 0.3 | uncertain | no |
| 9 | Corporate Europe Observatory | 5 | 26119 "Diversify EU" instrument | 0.4 | uncertain | no |
| 10 | DIGITALEUROPE | 4 | 25527 Dual-Use Regulation evaluation | 0.8 | match | yes |
| 11 | DIGITALEUROPE | 5 | 25280 Horizon Europe joint undertakings | 0.7 | match | yes |
| 12 | Egmont | 3 | 25896 International Procurement Instrument | 0.8 | match | no (borderline) |
| 13 | Eurocities | 4 | 26171 Atlantic macro-regional strategy | 0.6 | match | yes (borderline) |
| 14 | ECRE | 1 | 25894 Schengen visa code recast | 0.8 | match | yes |
| 15 | European Environmental Bureau | 2 | 26097 early warning report on waste | 1.0 | match | yes |
| 16 | European Policy Centre | 1 | 26119 "Diversify EU" instrument | 0.9 | match | yes |
| 17 | European Policy Centre | 2 | 25397 Europol Regulation | 0.8 | match | no (borderline) |
| 18 | European Policy Centre | 3 | 24198 Cloud and AI Development Act | 0.8 | match | yes |
| 19 | European Policy Centre | 5 | 25527 Dual-Use Regulation evaluation | 0.8 | match | no |
| 20 | FGTB-ABVV | 2 | 25923 omnibus on taxation | 0.6 | match | yes (tax fairness) |
| 21 | IEEP | 2 | 26097 early warning report on waste | 1.0 | match | yes |
| 22 | IEEP | 3 | 26171 Atlantic macro-regional strategy | 0.7 | match | yes (borderline) |
| 23 | Rud Pedersen Brussels | 2 | 26119 "Diversify EU" instrument | 0.8 | match | yes (horizontal, consultancy) |
| 24 | SEC Newgate EU | 5 | 25527 Dual-Use Regulation evaluation | 0.5 | uncertain | no |
| 25 | Transparency International EU | 1 | 25981 public procurement | 0.9 | match | yes |
| 26 | Transparency International EU | 4 | 25402 Eurojust Regulation | 0.7 | match | no (borderline) |
| 27 | VBO FEB | 1 | 24198 Cloud and AI Development Act | 0.8 | match | yes |
| 28 | WWF European Policy Programme | 1 | 25794 ETS directive | 1.0 | match | yes |
| 29 | industriAll Europe | 1 | 25280 Horizon Europe joint undertakings | 0.8 | match | no (borderline, as on 3 October) |
| 30 | logos public affairs | 4 | 25981 public procurement | 0.8 | match | yes (horizontal, consultancy) |

**Precision on the sample: 20 of 30 (67%), up from 17 of 30 (57%).** Counting the
borderline pairs as relevant: 25 of 30 (83%), up from 19 of 30 (63%). By band: `match` 20
of 27 (74%, was 13 of 25), `uncertain` 0 of 3 (was 4 of 5).

What the sample shows:

- The top of the list improved most. Rank 1 pairs: 8 of 9 relevant (v1: 1 of 6).
- Europol no longer leads generic profiles; the one Europol pair left (EPC) is borderline.
- The reranker's band now separates: the three uncertain pairs were all judged not
  relevant. Relevance 0.7 and 0.8 still holds misses (7 of the 10 misses), so the score is
  a ranking, not a guarantee.
- The gain is smaller than hoped (the review hoped for 75%). Think tanks and consultancies
  with every interest ticked still get a mix: the model, like the embedding, gives a broad
  profile the benefit of the doubt on mid-size files (dual use, procurement instrument).
- 30 pairs from one judge is a small sample: a 95% interval on 20 of 30 runs from about 49%
  to 81%, so the two samples overlap. The direction is consistent with the rank 1 numbers
  and the band split, not proven by them.

## Quotes verified

Quotes over all providers: 12,633 of 12,845 verified (98.3%, after the refresh of 5 October); the provider table is
further down. The first measurement, over the Gemini API analyses only: 240 submissions analysed
(161 from the feedback text, 46 from the English translation, 33 from an attachment) on 30
publications. The model returned 529 key-ask quotes; **511 were found verbatim** in the
source after normalisation (**96.6%**), 18 were dropped before storage, so no unverified
quote reaches a page. Stances: 181 wants changes, 19 supports, 7 opposes, 33 unclear. 33
analyses (14%) were held back for review (confidence under 0.6 or unclear) and are counted
on the page, not shown.

Organisation rows without text and without a readable attachment (questionnaire answers)
cost no call and are counted only: 502 such rows in the cache.

Critic over the first 5 drafts: 44 sentences, 42 supported by a retrieved chunk or the profile
(95.5%), 2 removed (each with an open `critic` review item).

## Calls per organisation

| What | Calls |
| --- | --- |
| Brief stage for one organisation (profile, match, landscape, lobby, reader, up to go / no-go) once the publications are precomputed | 0 (35 runs: min 0, mean 0.0, max 0) |
| Draft for one chosen consultation (drafter plus critic) | 2 |
| Runs with a draft | min 2, mean 2.6, max 4 (n = 5; the two runs of stage 2 also paid for summaries that were not cached yet) |
| Publication layer, one-off and shared by every organisation: summary | 1 per publication |
| Publication layer: analyst | 1 per organisation submission with text, capped at 20 per publication and pass |

Mean over all 40 runs: 0.33 calls per run. The whole open scope (41 summaries and every
organisation submission with text on them) cost 195 calls (41 summaries, 154 analyses; 489 rows without text were free), after which 40
organisations got their brief for 0 calls and 5 drafts cost 2 calls each.

## Cache hit rate

Reader summaries read by runs: **375 of 378 from the cache** on 6 October (193 of 196 on 3
October, before the 35 rebuilt runs). The 3 misses are the stage 2 runs made before the
precompute existed. Every publication summary and analysis is
cached forever (`publication_summaries`, `feedback_analyses`), so a second pass over the
same scope costs nothing: the open-scope precompute of this stage re-read 41 publications
and called the model only for the 7 summaries and 47 analyses that were missing.

## Timings

Model latency (from `llm_calls`, `gemini-3.5-flash-lite`):

| Purpose | Calls | Median | p95 | Max |
| --- | --- | --- | --- | --- |
| draft | 5 | 2.4 s | 2.5 s | 2.5 s |
| critic | 5 | 2.0 s | 2.0 s | 2.0 s |
| reader summary (model time only) | 5 | 2.0 s | 2.0 s | 2.0 s |
| feedback analysis (model time only) | 87 | 1.2 s | 1.5 s | 1.9 s |

Until the middle of this stage the logged duration included the wait for a token from the
shared bucket, so the 41 summaries and 153 analyses logged earlier show the 10 calls per
minute pacing (median about 6 s), not the model. The log now starts the clock after the
token is granted; the rows above for summary and analysis are the 92 calls logged after
that fix. Over the whole day 3 analyst calls were retried after a transient error and
succeeded; 0 calls failed.

Wall clock: open-scope precompute pass 277 s for 54 calls (5.1 s a call, the budget pace);
the 38 demo runs up to go / no-go in 22 s in total (graph nodes median 0.1 s); a decision
to draft (drafter plus critic plus memory) 5 to 6 s from the command line.

## Precompute over the V1 scope

`make precompute` (`inspraak precompute --feedback-limit 20`) walks the whole V1 scope:
the 41 open publications by nearest deadline, then the 40 recently closed ones, most
recently closed first. Today's passes:

| Pass | Calls | Time | Result |
| --- | --- | --- | --- |
| open publications, first attempt (cut off with the previous session) | 128 | about 12 min | 32 of 41, in the 33rd |
| open publications, resumed | 54 | 277 s | 41 of 41, nothing skipped |
| whole scope | 92 | 493 s | 45 of 81, then `daily budget of 300 calls is spent` |

The budget stop is clean: the pass logs the reason, writes nothing half done and exits 0.
Calls logged today: 296 ok, 0 failed (plus 2 probe calls before the log existed). State
at the stop: 46 summaries (41 open, 5 closed), 742 analysis rows (240 analysed, 502
without text) on 37 publications.

**Finished the same evening with the batch providers.** The Gemini API budget was the
bottleneck, so the backfill moved to the two batch providers (`scripts/backfill.py`):

| Pass | Provider | Submissions | Time | Errors |
| --- | --- | --- | --- | --- |
| single calls, then guarded batches of 10 (stopped by hand) | Claude Haiku, batch | 1,328 analysed | 17 min | 0 |
| guarded batches of 10, 30 in parallel | Gemini Flash, batch | 4,897 analysed, 503 calls | 34 min | 0 |
| single calls for the rows a batch skipped or could not tie | Gemini Flash, batch | 30 | 2 min | 0 |
| summaries of the 35 closed publications | Gemini Flash, batch | 35 calls | 14 min | 0 |

Result: 0 organisation submissions left in scope. 9,117 analysis rows: 6,505 with a model
analysis, 2,612 without readable text. 81 of 81 publications have a summary.

**Batch guard.** Every answer in a batch must echo the opening words of its own
submission; the code checks them against that submission's text and discards an answer
that does not match, so a stance can never land on the wrong submission. Quotes are
verified against the submission's own text as before.

**Provider check before the switch.** Haiku re-analysed 20 submissions the Gemini API
had already analysed: the same stance in 18 of 20, and 50 of 50 Haiku quotes verified
(Gemini: 45 of 46). The two disagreements were "wants changes" (Gemini) against
"supports" (Haiku) on submissions that support the proposal and ask for amendments.

**Quotes by provider** (proposed, dropped):

| Provider | Quotes | Dropped | Verified |
| --- | --- | --- | --- |
| Gemini Flash, batch | 9,237 | 35 | 99.6% |
| Gemini Flash Lite, API | 529 | 18 | 96.6% |
| Claude Haiku, batch | 3,079 | 159 | 94.8% |

778 analyses have a low confidence or an unclear stance (812 when first counted on 3
October, 776 before the refresh of 5 October). They are held back from the pages and wait in the review queue.

**Drafts redone.** The four drafts written before the stricter critic (Zero Waste Europe,
Transport and Environment, EDRi, ETF) were rejected and written again with Gemini Flash.
The critic kept 29 of 40 sentences and removed 11, each listed with its reason. With the
BEUC draft: 38 of 49 sentences supported (77.6%) across the 5 current drafts.

## What is not measured

- Precision is one judge's verdict on 30 pairs per matcher version; no second rater, and
  the v2 judge (the coding agent) was not blind to the matcher's output.
- Draft quality beyond sentence support (tone, whether an organisation would send it) is
  not scored. The drafts are labelled "machine draft, awaiting human review".
- The Anthropic provider is not exercised (no key).
- Privacy: non-citizen rows keep the organisation name the submitter typed. A few look like
  sole traders or micro firms (a one-person shop filing as COMPANY or OTHER). The guard
  classifies by declared user type and the presence of an organisation name, as the SPEC
  says; it cannot tell a trading name from a person's name. The about page says so.

## Refresh of 5 October

Have Your Say and the meeting list were fetched again on 5 October 2026 (finished 23:30
Brussels time); the register was not downloaded again. `inspraak ingest hys --skip-closed`
with the 20 hour page cache, 138 requests, all cache misses. The scope stayed at 81
publications: no consultation opened in the 90 day window since 3 October, and the 41
open ones were all still open at the time of the fetch.

| What | 3 October | 5 October |
| --- | --- | --- |
| Submissions on the 41 open publications | 1,038 (643 from organisations) | 2,503 (681 from organisations) |
| Child Safety Online (26173), submissions | 84 | 1,507 (15 organisations) |
| Public procurement (25981), deadline | 27 November | 30 November |
| Organisation submissions in scope, analysis rows | 9,117 | 9,155 |
| New organisation rows | | 38: 26 analysed in 3 batched calls (Gemini Flash), 12 without readable text |
| Quotes verified | 12,585 of 12,797 | 12,633 of 12,845 |
| Meetings | 11,527 | 11,531 (file of 5 October) |

The 11 new attachments were downloaded and parsed (52 pages). Reloading the meetings
file empties `publication_meetings` (foreign key cascade), and the backfill's summary pass
rebuilt it for all 81 publications (4,825 rows). The rerank cache held every pair, so the
35 rebuilt briefs and 5 new briefs for the organisations with drafts cost 0 model calls.

