# Strata

A fact knowledge layer that treats every fact as a dated, scoped, grounded
claim, so contradiction becomes a decision rather than a guess.

A fact is not a string. It is a claim with coordinates: entity, metric, period,
scope, basis and unit, carrying provenance (publisher, publication date) and a
quote that was re-found on the cited page. Two claims contradict *only* if every
coordinate matches and the values still differ. If a coordinate differs they do
not contradict, they reconcile, and the differing coordinate *is* the
explanation.

**Status: scaffold. Configuration, storage, the provider adapter and the
response cache exist; the pipeline arrives in later phases.**

## Setup and Run Instructions

```bash
uv sync --extra dev
cp .env.example .env
uv run uvicorn strata.api:app --reload
```

Open http://127.0.0.1:8000. `GET /health` reports the active provider, model
and storage backend. Tests: `uv run pytest`.

Storage is a local SQLite file by default. Set `STRATA_DB=turso` together with
`TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` to run the same schema on Turso.
Extraction uses Gemini by default; set `STRATA_PROVIDER=ollama` to use a local
vision model through Ollama instead.

## Video Demo

_Pending._

## Approach

_Pending._

## Limitations and Next Steps

_Pending._

## Additional Notes

_Pending._

## Repository layout

```
strata/            the package, one module per stage
  providers/       extraction adapters: gemini (shipped), ollama (local)
  cache.py         model responses keyed by content hash, committed
  schema.sql       plain SQL, identical on SQLite and libSQL
web/               vanilla JS + Tailwind CDN, no build step
tests/
data/              database and response cache
starter-datasets/  the six PDFs shipped with the assignment
```
