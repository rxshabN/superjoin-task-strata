# Strata

A fact knowledge layer that treats every fact as a dated, scoped, grounded claim, so
contradiction becomes a decision rather than a guess.

A fact is not a string. It is a claim with coordinates: entity, metric, period, scope, basis
and unit, carrying provenance (publisher, publication date) and a quote that was re-found on
the cited page. Two claims contradict *only* if every coordinate matches and the values still
differ. If a coordinate differs they do not contradict, they reconcile, and the differing
coordinate *is* the explanation.

Hosted demo: `https://strata-600642773754.asia-south1.run.app` (Cloud Run, backed by Turso; uploads and questions are live).
The repository also runs with no key at all, on the committed corpus.

## Setup and Run Instructions

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
git clone <REPO_URL> strata && cd strata
uv sync
uv run uvicorn strata.api:app --reload
```

Open http://127.0.0.1:8000. This needs no API key: `data/strata.db` holds the six starter
PDFs fully processed, and every model response is in `data/cache/`. Browse the documents,
facts, relations, quarantine and the Answer view; the four demo questions on the Answer view
replay from the cache too. Tests: `uv run pytest` (189 tests, about four seconds).

To process new PDFs, or to ask a question that is not cached, the server needs a model.
Copy `.env.example` to `.env` and pick one:

- `GEMINI_BACKEND=aistudio` with a free `GEMINI_API_KEY` from AI Studio. The free tier
  allows 5 requests a minute and 20 a day per model, and failed requests count, so the
  client retries 429 twice and 503 never.
- `GEMINI_BACKEND=vertex` with `GOOGLE_CLOUD_PROJECT` set and `gcloud auth
  application-default login` done. No daily cap. This is what built the corpus.
- `STRATA_PROVIDER=ollama` with a local vision model, for a fully offline run.

An upload can also carry its own key for that request only, in the `X-Gemini-Key` header or
the optional field on the upload form; it is never stored. Uploads are capped at 20 MB and
100 pages (`STRATA_MAX_UPLOAD_MB`, `STRATA_MAX_UPLOAD_PAGES`), and a document is sent to the
model in 50-page slices, one request each.

Rebuilding the corpus from scratch replays the cache and makes zero requests:

```bash
uv run python scripts/build_corpus.py --extract --consolidate
```

Docker: `docker build -t strata . && docker run -p 8000:8000 strata`.

Hosted: set `STRATA_DB=turso`, `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`, seed the
database once with `uv run python scripts/seed_turso.py`, then deploy the container.
The demo runs on Cloud Run in the same GCP project as Vertex AI, so the service account
authenticates to Gemini without any key:

```bash
gcloud run deploy strata --source . --region asia-south1 --allow-unauthenticated \n  --min-instances 1 --max-instances 2 --memory 2Gi --cpu 1 --no-cpu-throttling --timeout 300 \n  --set-env-vars GEMINI_BACKEND=vertex,GOOGLE_CLOUD_PROJECT=<PROJECT>,GOOGLE_CLOUD_LOCATION=global,STRATA_DB=turso,STRATA_REPLICA_PATH=/tmp/replica.db,TURSO_DATABASE_URL=<URL>,TURSO_AUTH_TOKEN=<TOKEN>
```

One minimum instance keeps the demo awake, and CPU stays allocated after a response so the
background pipeline runs at full speed. Uploads and their PDF bytes persist in Turso across
restarts and redeploys. On the hosted demo a 23-page presentation went from upload to ready in
65 seconds, model time included.

API, all JSON: `GET /health`, `GET /documents`, `POST /documents` (multipart `file`),
`GET /claims`, `GET /claims/{id}`, `GET /relations`, `GET /relations/{id}`,
`GET /quarantine`, `GET /metrics`, `GET /entities`, `GET /answer?entity=&metric=&period=`,
`POST /ask` (`{"question": "..."}`), `GET /pages/{doc}/{page}.png?claim=` for a page
render with the claim's evidence highlighted. The UI is static files over the same API,
and every view is addressable: `#relation/140`, `#claim/301`,
`#answer?q=What%20was%20India%27s%20GDP%20growth%20in%20FY25%3F`.

## Video Demo

`<VIDEO_URL>`

| Time | What is shown |
|---|---|
| `<t>` | A PDF from outside the starter set processed live |
| `<t>` | Case 1, corroboration across documents |
| `<t>` | Case 3, an apparent contradiction explained by basis, then the Answer view |
| `<t>` | Case 2, a genuine contradiction |
| `<t>` | Case 4, an extraction failure and the quarantine |

## Approach

### The thesis

Most fact extractors produce strings, embed them, and flag pairs of numbers that differ.
That is a contradiction detector with no notion of *why*, and it raises a false alarm on
every document that measures the same thing two legitimate ways. Strata extracts claims with
coordinates instead, and puts one small decision procedure at the centre. One mechanism
produces all three relationship kinds the brief asks for; there are no separate detectors.

Grounding is verified, not asserted. Every claim the model emits carries a verbatim quote,
and the quote must be re-found in the text layer of the cited page before the claim becomes a
fact. What cannot be re-found goes to a visible quarantine with a typed reason. That gate is
the anti-hallucination mechanism, and it is the fourth required case running as a feature.

### Pipeline

Five stages. Only the second uses a language model; every judgement the system makes happens
in ordinary Python afterwards, so all of it can be rerun from the response cache for free.

| Stage | What it does | Writes |
|---|---|---|
| Ingest | PyMuPDF text per page, word counts, and three regex hints per page: a unit declaration such as "All amounts in Indian Rupees in million", a consolidated/standalone marker, a period header. Pages under 40 words are skipped. A document is keyed by SHA-256, so re-uploading is a no-op. | `documents`, `pages` |
| Extract | 50-page slices sent as native PDF to Gemini 3.8 Flash with the page hints and the metric keys seen so far. One JSON object per line: subject, metric key, the document's own label, value, unit, period, scope, basis, a verbatim quote, and identifiers such as DIN or CIN. The first slice also returns a document line: title, publisher, publication date. Temperature 0, fixed seed, raw response cached by content hash. | `claims` |
| Verify | The quote is normalised and searched on the cited page (`exact`), on the neighbouring pages (`nearby`), then as a bag of numbers and words (`tokens`). Pass: grade, character span and bounding boxes stored. Fail: quarantine with a reason. | `evidence`, `quarantine` |
| Canonicalise | Period strings become date intervals (FY25, 2024-25 and FY2024/25 are one interval). Units become a base unit and scaled value, with the printed precision kept for tolerance. Entities resolve by identifier first, then by normalised name. Basis is read from the claim, from markers like "(P)" or "(AE)", or from phrases in the quote. Metric keys join a registry that grows per document. Block key = entity, metric, interval. | `entities`, `metrics`, `claim_canon` |
| Reconcile | Every pair inside a block goes through the decision procedure below. When a document is added, only the blocks it touches are recomputed. | `relations` |

### The decision procedure

For two numeric claims that share entity, metric and period, with units already in base
terms:

1. If the values agree within the printed precision, they **corroborate**. Any scope or basis
   difference is noted on the relation rather than blocking it.
2. If both state a scope and the scopes differ, they are **reconciled by scope**. Standalone
   against consolidated is the textbook case.
3. If the bases differ and both are ranked (advance estimate < provisional < revised <
   actual), the more mature basis **supersedes** the other. Projections and budgets are not
   ranked; they only ever reconcile.
4. If one side states no scope and the other does, they are reconciled by scope at low
   confidence: unstated never contradicts.
5. If both come from the same publisher: different labels mean two line items under one key,
   reconciled at low confidence; a later publication date means a restatement, which
   supersedes; otherwise a contradiction for review.
6. Otherwise it is a **contradiction for review**: different publishers, every coordinate
   matched, values differ. Provenance never blocks a comparison; it only decides which of two
   disagreeing claims is current.

String-valued claims (a director's role, a rating) block on entity and metric only. Equal
values corroborate; different values with different as-of dates supersede by time; the same
as-of date is a contradiction. A DIN printed beside a name is the entity key, so two documents
agree on who is meant.

Confidence has one meaning per level: `high` is the procedure's own verdict, `low` marks a
heuristic (unstated scope, two line items), `review` marks a contradiction a person should
look at.

### The Answer view

The Answer view is the extension on top: ask for an entity, metric and period and it returns
the current claim, everything that claim superseded and why, what corroborates it, and what
contradicts it. Nothing is generated; it is a lookup over the relations table. "Current" is
chosen by basis maturity, then by how many other claims corroborate it, then by publication
date. A question in plain English costs one model request to turn the words into those three
coordinates, from the registry and entity list; the model never sees the facts, and the page
shows how the question was read. This is the direct answer to knowledge bases that quietly go
stale: staleness is modelled, not accidental.

### The four cases, from real output

Nothing below is encoded anywhere. The ids are rows the engine produced on the six starter
PDFs; every link opens the evidence with the quote highlighted on the rendered page.

**Case 1, corroborated across documents.** [`#relation/140`](https://strata-600642773754.asia-south1.run.app/#relation/140).
The FY24 annual report, page 36: "Revenue from contracts with customers 81,415.38", in INR
million, consolidated. The Q4 FY24 earnings deck, page 6: "₹8,142 Cr FY24 revenue from
services". Different unit systems, different phrasing, different scope wording. In base units
the two agree within the printed precision of the crore figure, so the engine calls them one
fact and notes that the scope wording differed.

**Case 3, an apparent contradiction explained by context.** Two instances.
By basis, [`#relation/300`](https://strata-600642773754.asia-south1.run.app/#relation/300): the Economic Survey (January 2025,
page 4) says "As per the first advance estimates of national accounts, India's real GDP is
estimated to grow by 6.4 per cent in FY25"; the IMF Article IV (November 2025, page 3) says
"economic growth of 6.5 percent in FY2024/25". Same entity, metric, period and unit, values
differ, but the Survey labels its figure an advance estimate, so the IMF figure supersedes it
with the reason attached. The RBI annual report's provisional 6.5 supersedes the same claim
([`#relation/299`](https://strata-600642773754.asia-south1.run.app/#relation/299)). Ask the Answer view for India's GDP growth
in FY25 and you get 6.5, with both 6.4 claims kept as history. A retrieval system would
happily answer 6.4.
By scope, [`#relation/156`](https://strata-600642773754.asia-south1.run.app/#relation/156): the same annual-report page carries
"Revenue from Operations" as 74,540.82 (standalone) and 81,415.38 (consolidated). Not a
contradiction; the scope coordinate differs, and that is the explanation shown.

**Case 2, a genuine contradiction.** [`#relation/244`](https://strata-600642773754.asia-south1.run.app/#relation/244). The RBI
annual report, page 17: "CPI inflation for 2025-26 is projected at 4.0 per cent". The IMF,
page 13: "Headline inflation is expected to remain benign and average 2.8 percent in
FY2025/26". Two institutions, same year, same measure, both projections, 4.0 against 2.8.
Different publishers never supersede each other, so the engine flags the pair for review and
shows both spans. The corpus holds 15 such cross-document contradictions, all for review;
another is the Survey's first advance estimate of FY25 growth (6.4) against the RBI's second
advance estimate (6.5), which the engine cannot order because both carry the same basis label
([`#relation/297`](https://strata-600642773754.asia-south1.run.app/#relation/297)).

**Case 4, an extraction failure and how it is handled.** Two shapes.
[`#claim/301`](https://strata-600642773754.asia-south1.run.app/#claim/301): a chart on deck page 9 whose text layer reads
"4,191 4,552 5,077 FY22 FY23 FY24 Express Parcel revenue". The page has lost the binding
between year and value, so the gate can prove 5,077 is on the page but not that it belongs to
FY24. The claim is accepted at the `tokens` grade and shown as weakly grounded; 59 claims in
the corpus sit there. [`#claim/190`](https://strata-600642773754.asia-south1.run.app/#claim/190): the model read "81,415 FY24"
off an infographic and cited page 11, where it is not. The quote was not found on that page or
its neighbours, so the claim never became a fact and sits in the quarantine with its reason;
the same figure entered the layer from page 22, where it could be verified. 26 claims are
quarantined, each with a typed reason.

A fifth relation worth a look: the 2022 prospectus lists Suvir Suren Sujan as a
non-executive nominee director and the FY24 annual report records his resignation on
August 24, 2023. Matched on DIN 01173669, the later state supersedes the earlier one by time
([`#relation/198`](https://strata-600642773754.asia-south1.run.app/#relation/198)).

### Engineering decisions and trade-offs

- **One model stage, four deterministic stages.** Extraction is the only place a model
  reads pages. Verification, canonicalisation and reconciliation are plain Python with 188
  tests, so their behaviour is predictable and a bug found late costs code, not quota.
- **No embeddings, no graph database.** Comparison happens inside blocks keyed by entity,
  metric and date interval. This is narrower than similarity search and that is the point:
  when the engine says two claims disagree, it can say on what.
- **The quote gate over trusting the model.** A verbatim quote that must be re-found costs
  recall on charts and infographics (they become `tokens` or quarantine) and buys the
  guarantee that every fact points at a real span on a real page.
- **Native PDF slices, not page images or plain text.** The model sees layout, and the
  50-page slice with an 8-claims-per-page cap keeps output far under the 65K token ceiling.
  Truncated responses drop the last page and resume from it.
- **Committed database and response cache.** The whole corpus and every model response are in
  the repository, so a reviewer sees the real output without a key, and a cold rebuild makes
  zero requests. The cost is 29 MB in git.
- **SQLite locally, the same schema on Turso hosted.** Plain SQL, no ORM, one file to read.
- **Registry consolidation is one request with a validator.** After the corpus is built the
  model sees the whole registry once and proposes merges; the code accepts a merge only if
  both keys exist, the unit classes agree, and no chains form. Merges are stored as aliases and
  survive rebuilds. On the corpus it proposed two, both accepted, and declined to merge
  `revenue_from_operations` with `revenue_from_contracts_with_customers`, which is a
  defensible accounting distinction, so those stay separate.
- **Gemini 3.8 Flash at temperature 0 with a fixed seed.** Tested on the earnings deck: 3.8
  and 3.7 Flash gave 78 to 79 well-grounded claims, 3.6 fewer, the Lite models invented a
  "consolidated" scope on every claim and were rejected for extraction.

AI tools used: Claude Code for planning and implementation; Gemini 3.8 Flash for extraction,
the registry consolidation pass, and reading plain-English questions.

## Limitations and Next Steps

| Limitation | What happens today | Next step |
|---|---|---|
| Chart pairings cannot be verified from a text layer that has lost them. | Such claims are accepted at the `tokens` grade and visibly marked; a mis-paired value contradicts the exact figure elsewhere and lands in review. | Render the chart region and ask the model to re-read only it, then re-verify. |
| Metric keys drift. The model sometimes keys the same measure two ways, and sometimes keys two measures the same way (`cpi_inflation` holds both CPI-Combined and CPI-IW; `headcount` holds workforce and ESOP holders). | Registry hints in every prompt reduce it; the consolidation pass merges what it is sure of; the registry view shows the rest with counts and aliases. | A manual merge and split in the UI, recorded in the same alias table. |
| Attribution is not extracted. The Economic Survey quotes the IMF's FY26 inflation projection; the claim carries the Survey as publisher, so its pair against the IMF's own later figure reads as a cross-publisher contradiction when it is the IMF revising itself. | Stated. The pair is still shown for review with both spans. | An `asserted_by` field on the claim, distinct from the document's publisher. |
| Publication dates come from the model reading the cover pages. The RBI report came back without one. | Its supersession rests on basis alone; the Documents view says "date not found". PDF metadata carries the right month for all six files, but it is a guess about provenance and is not used. | Offer the metadata date as a suggestion the user confirms. |
| The fiscal year is assumed to end in March. | A document on another convention produces intervals that never match, so it misses comparisons rather than inventing them. | A per-document convention read from the first slice. |
| State facts use one small rule. | Appointment to resignation works, matched on DIN. Chains with more than two steps, or role changes without a date, are out of scope. | Model roles as intervals with start and end. |
| Scanned PDFs have no text layer. | Every claim on such a page is quarantined as `no_text_layer`; nothing is invented. | OCR the page image before verification. |
| Thinking models are not deterministic at temperature 0. | The same deck gave 78 and then 62 claims on the same prompt in two runs. The committed cache is what makes the corpus reproducible. | Nothing to fix; stated so the numbers are read correctly. |
| Two domains were tested in depth. | A 30-page arXiv paper from outside both domains extracted cleanly (40 claims, 30 exact, 1 quarantined) and produced no relations, because 38 of its claims carry no period or unit and nothing in the layer measures the same thing. That is the honest generalisation result: the layer does not invent comparisons it cannot ground. | More conventions in the period and unit parsers as new document types arrive. |
| Free-tier quota. | On an AI Studio key a 100-page upload is two of the day's twenty requests; the UI says when the quota is gone and accepts a caller's own key. The hosted demo runs on Vertex AI and has no daily cap. | Nothing to fix. |

## Additional Notes

**Numbers.** Six documents, 511 pages, two domains. 956 claims extracted, 930 grounded (861
exact, 10 nearby, 59 tokens), 26 quarantined. 224 metric keys with 2 aliases, 112 entities,
463 relations. Across documents: 46 corroborate, 37 reconcile, 12 supersede, 15 contradict.

**Cost and time.** The corpus took 11 extraction requests in one pass (989 s, mostly model
time) plus one consolidation request, about ₹100 of Vertex AI credit. Everything after that
is free: a full zero-request rebuild of canonical rows and relations takes 1.5 s; ingesting
all 511 pages takes 1.4 s; a page render with highlights takes 0.08 s; an Answer lookup 9 ms.
Adding a 10-page document to the existing layer took one request (89 s of model time), then
0.6 s to verify, canonicalise and reconcile, touching 24 of 617 blocks and producing 27
cross-document relations. Re-uploading a known PDF takes 3 ms and no request.

**Why the database and cache are committed.** The brief says a paid service should come with
enough sample output to evaluate without an account. Committing the processed corpus and
every raw model response does that literally: the repository demonstrates itself, and every
number in this README can be checked by opening the UI.

**What would need to be true to trust this outside these two domains.** The period parser
would need the target's fiscal conventions; the unit parser would need its currencies and
scales; the model would need a few pages to seed the registry with that domain's measures.
None of that is document-specific, and none of it is hardcoded, but each is a place where a
new domain can silently miss comparisons until it is added.

**Repository layout.**

```
strata/            the package, one module per stage
  ingest.py        text, hints, thin-page skip, SHA-256 registration
  extract.py       slicing, the prompt, NDJSON parsing, truncation resume
  verify.py        the quote gate: grades, spans, bounding boxes, quarantine
  canon.py         periods, units, values, entities, basis, scope, the registry
  reconcile.py     the decision procedure
  consolidate.py   the one-request registry pass and its validator
  ask.py           plain-English question to coordinates
  queries.py       read models for the API, including the Answer view
  api.py           FastAPI routes and page rendering
  providers/       gemini (AI Studio or Vertex) and ollama adapters
  cache.py         model responses keyed by content hash, committed
  schema.sql       plain SQL, identical on SQLite and libSQL
web/               vanilla JS and Tailwind over the API, no build step
scripts/           build_corpus.py, seed_turso.py
tests/             189 tests
data/              strata.db and cache/
starter-datasets/  the six PDFs shipped with the assignment
```
