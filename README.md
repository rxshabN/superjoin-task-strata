# Strata

Strata is a fact knowledge layer for PDFs. Upload documents, and it pulls out the facts, ties each one to
the exact place on the page it came from, and works out which facts agree, which disagree, and which only
look like they disagree.

The one idea behind it: a fact is not a string, it is a claim with coordinates. Entity, metric, period, scope,
basis and unit, plus who published it and when, plus a quote that was re-found on the cited page. Two claims
contradict only if every coordinate matches and the values still differ. If a coordinate differs, they do
not contradict; that coordinate is the explanation.

Live demo: `https://strata-600642773754.asia-south1.run.app` (Cloud Run + Turso).
The repository also runs locally with no API key, on the committed corpus.

## Setup and Run Instructions

Needs Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/rxshabN/superjoin-task-strata.git strata && cd strata
uv sync
uv run uvicorn strata.api:app --reload
```

Open http://127.0.0.1:8000. No API key is needed for this: `data/strata.db` already holds the six starter
PDFs fully processed, and every model response is in `data/cache/`, so the four demo questions on the
Answer view replay from cache as well. Tests: `uv run pytest` (249 tests, about fifteen seconds).

To upload new PDFs or ask new questions, the server needs a model. Copy `.env.example` to `.env` and pick one:

- `GEMINI_BACKEND=aistudio` with a free `GEMINI_API_KEY`. The free tier allows 5 requests a minute and 20 a
  day, and failed requests count, so the client retries 429 twice and 503 never.
- `GEMINI_BACKEND=vertex` with `GOOGLE_CLOUD_PROJECT` set and `gcloud auth application-default login` done.
  No daily cap. This is what built the corpus.

An upload can also carry its own key for that one request (the `X-Gemini-Key` header, or the optional field
on the form); it is never stored. Uploads are capped at 20 MB and 100 pages and go to the model in 50-page
slices, one request each. Password-protected, empty and non-PDF files are refused with a reason. A document
that ends with no claims, a cut-off response or an exhausted quota says so on its row, and uploading the same
file again re-runs it. Questions are capped at 500 characters.

Rebuild the corpus from scratch (replays the cache, zero requests, about ten seconds):

```bash
uv run python scripts/build_corpus.py --extract --consolidate
```

Docker: `docker build -t strata . && docker run -p 8000:8000 strata`.

## Video Demo

`https://drive.google.com/file/d/1ww8Sy_53iyUPZQMeAkwQqWBJREXUR9dy/view?usp=sharing`

### What is shown

- A PDF from outside the starter set processed live
- Case 1, corroboration across documents
- Case 3, an apparent contradiction explained by basis, then the Answer view
- Case 2, a genuine contradiction
- Case 4, an extraction failure and the quarantine

## Approach

### The idea

Most extractors produce strings, embed them, and flag pairs of numbers that differ. That raises a false alarm
on every document that measures the same thing two legitimate ways, and it cannot say why two figures differ.
Strata extracts claims with coordinates and runs one small decision procedure over them. That single
mechanism produces all three relationship kinds; there are no separate detectors.

Grounding is verified, not trusted. Every claim the model returns must include a verbatim quote, and the quote
has to be found again on the cited page before the claim counts as a fact. Anything that cannot be found goes
to a visible quarantine with a reason.

### How it works

Five stages. Only the second one uses a language model; everything after it is plain Python, so all of it can
be rerun from the cached responses.

1. **Ingest.** PyMuPDF reads the text of each page and picks up three hints per page: a unit declaration such
   as "All amounts in Indian Rupees in million", a consolidated or standalone marker, and a period header.
   Near-empty pages (under 40 words and fewer than three numbers) are skipped. Documents are keyed by SHA-256,
   so re-uploading one is a no-op.
2. **Extract.** 50-page slices go to Gemini 3.8 Flash as native PDF, with the page hints and the metric keys
   seen so far. It returns one JSON object per fact: subject, metric key, the document's own label, value,
   unit, period, scope, basis, a verbatim quote, and any identifiers printed with the entity (a DIN, a CIN).
   The first slice also returns the document's title, publisher and date. Temperature 0, fixed seed, and
   every raw response is cached under a hash of the slice and the prompt.
3. **Verify.** The quote is normalised and searched on the cited page (`exact`), then on the neighbouring
   pages (`nearby`), then as a bag of numbers and words (`tokens`). A hit stores the grade, character span and
   bounding boxes. A miss goes to quarantine with a reason.
4. **Canonicalise.** Periods become date intervals (FY25, 2024-25, FY2024/25, Q3-2024, 2025:Q2, 30-Sep-24,
   "twelve months ended September 28, 2024" all resolve; an unreadable period leaves the claim non-comparable
   rather than failing the document). Units become a base unit and a scale, so 8,142 crore and 81,415.38
   million can be compared, with the printed precision kept as tolerance. A bare "million" on a money-like
   label takes the currency printed on its page. Entities resolve by identifier first, then by normalised
   name; "the Company" resolves to the publisher. Basis is read from the claim, from markers such as "(P)" or
   a trailing BE/RE, or from phrases in the quote. Metric keys join a registry that grows with each document.
   The block key is entity, metric and interval.
5. **Reconcile.** Every pair inside a block goes through the decision procedure below. Adding a document
   recomputes only the blocks it touches.

### The decision procedure

For two numeric claims with the same entity, metric and period, in base units:

1. Values agree within the printed precision: they **corroborate**. A scope or basis difference is noted on
   the relation, not used to block it.
2. Both state a scope and the scopes differ: **reconciled by scope**. Standalone against consolidated is the
   textbook case.
3. Same publisher, and the labels name different rows: two line items under one key, **reconciled by label**
   at low confidence. This runs before the basis rule so one release cannot make one series supersede another.
4. Bases differ and both are ranked (advance estimate < provisional < revised < actual): the more mature one
   **supersedes**. If one basis is unranked (a projection, a budget, a pro forma restatement) and the same
   publisher printed the later figure in a later publication, the later figure supersedes as a restatement;
   otherwise they reconcile by basis.
5. One side states no scope: reconciled by scope at low confidence. Unstated never contradicts.
6. Same publisher, later publication date: a restatement, which supersedes. Same publisher, same date: a
   contradiction for review.
7. Otherwise: a **contradiction for review**. Different publishers, every coordinate matched, values differ.
   Provenance never blocks a comparison; it only decides which of two disagreeing claims is current.

Text-valued claims (a director's role, a rating) block on entity and metric only: equal values corroborate,
different values with different as-of dates supersede by time, the same as-of date is a contradiction. A DIN
printed beside a name is the entity key, so two documents agree on who is meant.

Confidence has one meaning per level: `high` is the procedure's own verdict, `low` marks a heuristic (an
unstated scope, two line items), `review` marks a contradiction a person should look at.

### The Answer view

Ask for an entity, metric and period and you get the current claim, everything it superseded and why, what
corroborates it and what contradicts it. Nothing is generated; it is a lookup over the relations table.
"Current" is chosen by basis maturity, then by how many claims corroborate it, then by publication date. A
question in plain English costs one model request to turn the words into those three coordinates; the model
never sees the facts, and the page shows how the question was read. This is the answer to knowledge bases
that quietly go stale: staleness is modelled, not accidental.

### The four cases, from real output

Nothing below is hard-coded. The ids are rows the engine produced on the six starter PDFs, and each link opens
the evidence with the quote highlighted on the rendered page.

**Case 1, corroborated across documents.**
[`#relation/145`](https://strata-600642773754.asia-south1.run.app/#relation/145). The FY24 annual report,
page 36: "Revenue from contracts with customers 81,415.38", INR million, consolidated. The Q4 FY24 earnings
deck, page 6: "₹8,142 Cr FY24 revenue from services". Different units, different phrasing, different scope
wording. In base units they agree within the printed precision, so the engine calls them one fact and notes
that the scope wording differed.

**Case 3, an apparent contradiction explained by context.**
By basis, [`#relation/307`](https://strata-600642773754.asia-south1.run.app/#relation/307): the Economic
Survey (January 2025, page 4) says "As per the first advance estimates of national accounts, India's real GDP
is estimated to grow by 6.4 per cent in FY25"; the IMF Article IV (November 2025, page 3) says "economic
growth of 6.5 percent in FY2024/25". Same entity, metric, period and unit, values differ, but the Survey's
figure is an advance estimate, so the IMF figure supersedes it and the reason is attached. The RBI's
provisional 6.5 supersedes the same claim
([`#relation/306`](https://strata-600642773754.asia-south1.run.app/#relation/306)). Ask the Answer view for
India's GDP growth in FY25 and you get 6.5, with both 6.4 claims kept as history. A retrieval system would
answer 6.4.
By scope, [`#relation/161`](https://strata-600642773754.asia-south1.run.app/#relation/161): one annual-report
page carries "Revenue from Operations" as 74,540.82 (standalone) and 81,415.38 (consolidated). Not a
contradiction; the scope differs, and that is the explanation shown.

**Case 2, a genuine contradiction.**
[`#relation/253`](https://strata-600642773754.asia-south1.run.app/#relation/253). RBI annual report, page 17:
"CPI inflation for 2025-26 is projected at 4.0 per cent". IMF, page 13: "Headline inflation is expected to
remain benign and average 2.8 percent in FY2025/26". Same year, same measure, both projections, two
institutions, 4.0 against 2.8. Publishers never supersede each other, so the pair is flagged for review with
both spans. The corpus holds 15 such cross-document contradictions. Another is the Survey's first advance
estimate of FY25 growth (6.4) against the RBI's second advance estimate (6.5), which the engine cannot order
because both carry the same basis label
([`#relation/304`](https://strata-600642773754.asia-south1.run.app/#relation/304)).

**Case 4, an extraction failure and how it is handled.**
[`#claim/301`](https://strata-600642773754.asia-south1.run.app/#claim/301): a chart on deck page 9 whose text
layer reads "4,191 4,552 5,077 FY22 FY23 FY24 Express Parcel revenue". The binding between year and value is
lost, so the gate can prove 5,077 is on the page but not that it belongs to FY24. The claim is kept at the
`tokens` grade and shown as weakly grounded; 59 claims in the corpus sit there.
[`#claim/190`](https://strata-600642773754.asia-south1.run.app/#claim/190): the model read "81,415 FY24" off an
infographic and cited page 11, where it is not. The quote was not found on that page or its neighbours, so the
claim never became a fact and sits in quarantine with its reason. The same figure entered the layer from page
22, where it could be verified. 26 claims are quarantined, each with a typed reason.

### Decisions and trade-offs

- **One model stage, four deterministic ones.** Only extraction uses a model. Verification, canonicalisation
  and reconciliation are plain Python with 249 tests, so a bug found late costs code, not quota.
- **No embeddings, no graph database.** Comparison happens inside blocks keyed by entity, metric and date
  interval. Narrower than similarity search, on purpose: when the engine says two claims disagree, it can say
  on what.
- **The quote gate.** Requiring a verbatim quote that can be re-found costs recall on charts and infographics
  (they become `tokens` or quarantine) and buys the guarantee that every fact points at a real span on a real
  page.
- **Native PDF slices, not page images or plain text.** The model sees layout, and a 50-page slice with a cap
  of 8 claims per page stays well under the output token ceiling. A cut-off response drops its last page and
  resumes from it.
- **Committed database and response cache.** A reviewer would see the real output without an API key, and a cold rebuild
  makes zero requests. The cost is about 23 MB in git.
- **SQLite locally, the same schema on Turso when hosted.** Plain SQL, no ORM, one file to read.
- **Registry consolidation is one request with a validator.** After the build, the model sees the whole
  registry once and proposes merges; the code accepts a merge only if both keys exist, the unit classes agree
  and no chains form. It proposed two, both accepted, and declined to merge `revenue_from_operations` with
  `revenue_from_contracts_with_customers`, a defensible accounting distinction.
- **Gemini 3.8 Flash at temperature 0 with a fixed seed.** On the earnings deck, 3.8 and 3.7 Flash gave 78 to
  79 well-grounded claims, 3.6 fewer, and the Lite models invented a "consolidated" scope on every claim.

AI tools used: Claude Code for planning the implementation, UI mockup and debugging. Gemini 3.8 Flash for
extraction, the registry consolidation pass, and reading plain-English questions.

## Limitations and Next Steps

| Limitation | What happens today | Next step |
|---|---|---|
| Charts whose text layer has lost the year-to-value pairing. | The claim is kept at the `tokens` grade and marked; a mis-paired value contradicts the exact figure elsewhere and lands in review. | Render the chart region and ask the model to re-read only that. |
| Metric keys drift: one measure keyed two ways, or two measures under one key (`cpi_inflation` holds CPI-Combined and CPI-IW). A US GDP release filed 23 measures under `gdp_growth`. | Registry hints and the consolidation pass reduce it; the registry view shows the rest. Since the label rule runs before the basis rule, the 176 false supersessions that release produced became 24. The Answer view can still pick the wrong series inside such a block: for US Q2 2025 growth it returns real final sales (1.9) over real GDP (3.3). | Have the model put commodity, sector or price basis into `scope`; manual merge and split in the UI. Ranking tweaks alone did not help; each one tried broke a correct answer elsewhere. |
| Unit wording from the model decides comparability. | Apple's FY25 statement said "million" with no currency, so at first it shared no relation with the FY24 statement. Bare scales on money-like labels now take the page's currency (eleven relations appear); a figure the model leaves with no unit at all stays non-comparable. | Carry a table header's unit onto every figure; treat unit-less integers under count-like keys as counts. |
| The fiscal year is assumed to end in March, and periods are parsed in English. | A bare "fiscal 2024" becomes April 2023 to March 2024, so a question about Apple's fiscal 2024 misses; "31 décembre 2024" is non-comparable. Such documents miss comparisons rather than inventing them. | A per-document convention read from the first slice; month names in other languages. |
| Rates with different annualisation conventions share a block. | BEA's annualised 3.0 and the Bank of England's quarter-on-quarter 0.7 for the same quarter were flagged as a contradiction. | An annualisation coordinate read from "annual rate" or "q/q" wording, applied like scope. |
| Attribution is not extracted. | The Economic Survey quotes the IMF's projection; the claim carries the Survey as publisher, so the pair against the IMF's own later figure reads as cross-publisher when it is the IMF revising itself. | An `asserted_by` field distinct from the publisher. |
| Publication dates come from the model reading the cover. | The RBI report came back without one; its supersessions rest on basis alone. PDF metadata has the right month but is not used, because it is a guess about provenance. | Offer the metadata date for the user to confirm. |
| A question with no period cannot reach a numeric fact. | Block keys carry a date interval, so "What is the Bank Rate?" is read correctly and finds nothing. | Fall back to the latest period for that entity and metric. |
| Scanned pages and image tables have no text to verify against. | Every such claim is quarantined (`no_text_layer` or `quote_not_found`). Nothing is invented. | OCR before verification. |

## Additional Notes

**Numbers.** Six documents, 511 pages, two domains. 956 claims, 930 grounded (861 exact, 10 nearby, 59
tokens), 26 quarantined. 224 metric keys with 2 aliases, 112 entities, 473 relations. Across documents: 46
corroborate, 38 reconcile, 12 supersede, 15 contradict.

**Cost and speed.** A full rebuild from an empty database replays the cache
in about ten seconds and reproduces every row byte for byte. Ingesting all 511 pages takes 1.4 s, a page
render with highlights 0.08 s, an Answer lookup 9 ms. Under eight concurrent readers while a document was
being extracted, 3,078 requests all returned 200 with a 95th-percentile latency of 0.24 s. Adding a 10-page
document took one request (89 s of model time) and 0.6 s to verify, canonicalise and reconcile, touching 24
of 617 blocks. Re-uploading a known PDF takes 3 ms. On the hosted demo a 23-page deck goes from upload to
ready in about a minute.

**Why the database and cache are committed.** The assignment mentions that a paid service should come with enough sample
output to evaluate without an account. Committing the processed corpus and every raw model response does
that literally: every number in this README can be checked by opening the UI.

**Testing beyond the starter set.** Three passes, about 36 outside PDFs in total: MoSPI GDP press notes, more
Delhivery filings, a Berkshire Hathaway letter, a Fed projections release, the IPCC summary, a UN SDG report,
an ECB bulletin, an arXiv paper, a Raspberry Pi datasheet, an IRS form, Apple's FY24 and FY25 statements, two
BEA GDP releases, two Tesla decks, the UN World Population Prospects summary and 100 pages of the Bank of
England's Monetary Policy Report, plus synthetic files (encrypted, empty, truncated, scanned, French text,
US-style dates, lakh-crore figures, a restated comparative). Grounding held throughout: in the last pass, 599
of 611 claims were re-found exactly. What broke, all fixed and covered by tests:

- Parsing: a US-format date crashed a whole document; "2000-2019" was read as one fiscal year; "lakh crore" was
  scaled as crore; a Unicode minus made a negative into text; "Q3-2024", "3Q-2024" and "30-Sep-24" did not
  parse, so a Tesla deck had no comparable claim; "$M" lost its scale; a bare "million" with no currency became
  a count, so two Apple statements shared no relation.
- Reasoning: a restated comparative labelled pro forma only reconciled, so the Answer view kept the stale
  figure; a release that filed 23 measures under one key produced 176 confident supersessions until the label
  rule was moved ahead of the basis rule, which also removed two wrong supersessions in the starter corpus.
- Robustness: an encrypted PDF returned a 500; a KPI deck with short slides produced nothing because every
  slide was skipped; the character-span search was a backtracking regex that hung on repeated tokens and, with
  a process-wide lock, would have frozen later uploads (now a linear scan); two instances could process one
  document at once; a document interrupted mid-pipeline could never be re-run.

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
  providers/       gemini (AI Studio or Vertex)
  cache.py         model responses keyed by content hash, committed
  schema.sql       plain SQL, identical on SQLite and libSQL
web/               vanilla JS and Tailwind over the API, no build step
scripts/           build_corpus.py, seed_turso.py
tests/             249 tests
data/              strata.db and cache/
starter-datasets/  the six PDFs shipped with the assignment
```
