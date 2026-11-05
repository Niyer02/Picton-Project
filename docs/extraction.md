# Relationship extraction

[Project overview](../README.md)

All shell commands below run from the project root, after the editable install
described in the [setup guide](setup.md). Paths in prose are relative to that root.

## Extract relationships with OpenRouter

`picton_graph/extraction/extractor.py` reads masked chunks from DuckDB and uses
`openai/gpt-6-luna` through OpenRouter's Chat Completions endpoint. There are no
additional provider SDK dependencies. `prompts/extraction.md` contains the extraction instructions,
four complete input/output examples covering all nine relationships, and twenty
negative examples. The seven class definitions and nine canonical relationship
definitions are appended from `ontology/schema.json`, so the prompt uses our actual ontology.

Each request contains one masked chunk, its masked heading, the masked filing
issuer, the filing/document type and role, and filing/report dates. Original
company names, the identity mapping, source URLs, and company-bearing local IDs
are not added to the request. Existing product brands and other clues in the
masked text remain; name masking does not guarantee that identities cannot be
inferred. Exhibit authorship is not assumed to match the filing issuer.

The existing source is the reserved `{"type":"SourceDocument","name":"document"}`
reference, a `SourceDocument` node created from local metadata. The model can return the other six entity types and
all nine canonical edge types. It returns exact evidence quotes, optional literal
relationship periods, and product/service references on supply assertions.
Events require an evidenced time expression; the model does not invent timestamps
from filing dates. No standalone chunk tagger or inverse-edge extraction is used.

The `named-entities-v1` output format has no model-generated entity IDs. Each
entity contains `type`, `name`, `evidence_text`, `event_type` and `time_text`.
Relationship `source` and `target` contain exactly `type` and `name`, for example
`{"type":"Company","name":"Company J"}`. The supplied-item arrays contain
Product/Service names directly, such as `"supplied_product": ["silicon wafers"]`.
Every reference must match an extracted entity of the required type. The same
name can identify different entity types; separate events must have distinct
descriptive names. Python resolves company IRIs and derives other graph identifiers.

Preview a future request without credentials or model calls:

```sh
.venv/bin/python -m picton_graph.extraction.extractor --ticker NVDA --form 10-K --limit 1 --dry-run
```

When ready to actually call the model, set `OPENROUTER_API_KEY` in the process
environment and omit `--dry-run`. The script does not read or modify `.env`:

```sh
.venv/bin/python -m picton_graph.extraction.extractor --ticker NVDA --form 10-K --limit 5
```

These commands process stored corpus chunks. The [GEPA workflow](evaluation.md) instead
evaluates the curated gold excerpts. The default limit is one chunk, not one filing. Selection is newest
filings first, then document, section and chunk order. Use `--document-id` to
restrict selection to a specific local document; `--role exhibit` or `--role all`
includes exhibits. `--form` always refers to the parent filing's form. The script
does not filter chunks by inferred relevance. `--max-tokens` defaults to 8192;
increase it explicitly if a later sample exhausts the completion budget.

The API request uses strict JSON Schema and
[`require_parameters: true`](https://openrouter.ai/docs/guides/features/structured-outputs)
to require supporting provider endpoints. It specifies the chosen model without
a substitute model or web tools. Requests run sequentially with a 120-second
timeout. There are no automatic paid retries or LLM repair calls. An error,
refusal, truncated response, invalid JSON, or validation failure stops the run;
the failed result remains available for inspection.

Each run creates a new `data/extractions/<run-id>/` directory containing:

- `manifest.json`: status, counts, model, output-format version, prompt/ontology hashes and the local
  company identity mapping.
- One numbered JSON file per attempted chunk: source identifiers/URL/hash,
  original/masked text hashes, exact request, provider response (including usage
  when returned), validation result and any error. Preview files have requests
  only. Provider responses and local provenance are for review, not instructions.

Validation checks the JSON shape, ontology types/directions, references,
irreflexive supply/competition edges, duplicate assertions, ten-company scope,
evidenced entity names, quoted time expressions, and supplied product/service types.
Evidence matching accepts capitalization, ordinary punctuation and whitespace
differences, including newlines and trailing spaces. It requires the same words
in order and preserves numeric punctuation, signs, percentages and symbols such
as `C++`. Names, time expressions and supply annotations use the same comparison;
typed endpoint references and canonical Company labels remain exact.
The model is still asked to copy source text. Stored evidence records whether the
match was exact or formatting-normalized, and retains an exact original substring
with both sets of character offsets. Original text is never lowercased or stripped
in storage or requests. Missing or stale masks
stop selection rather than falling back to original text. Known Company entities
resolve to their existing graph IRIs; other internal IRIs are derived from the
chunk and typed name, independently of entity order.

These are **review candidates**, not Neo4j imports. DuckDB is opened read-only;
original HTML, chunks, masks and Neo4j remain unchanged. Validation checks strings
and structure, not semantic truth: a real quote can still be misinterpreted.
Manually label a sample, including empty results, before reporting an error rate
or importing assertions. Preserve temporal qualifiers when designing that import;
do not treat all historical evidence as current relationships.

MVP limits: no cross-chunk coreference, cross-document entity merging, OCR,
resumable batches, automatic repairs, or graph writes. A rerun creates a new run
and will call the model again for the selected chunks. Review/fix the selection
before rerunning after a partial failure. Provider results on the curated gold
set do not establish extraction accuracy across the full filing corpus.

Offline checks (temporary databases and mocked HTTP only):

```sh
.venv/bin/python -m unittest -v tests.test_extract_relationships
```

## Full-corpus extraction with parallel workers

`picton_graph/extraction/corpus.py` processes every stored masked chunk across all companies,
forms, primary filings and exhibits. It reuses the same prompt schema, source
mask checks and extraction validator. The recorded November 2026 corpus has
34,015 chunks from 1,664 documents. The selected prompt from the named-reference
GEPA run can be used when those local artifacts are available:

```sh
.venv/bin/python -u -m picton_graph.extraction.corpus --workers 8 \
  --prompt data/gepa/ca912a0b-311d-4cfd-8108-60d0fd5e0dc8/optimized_prompt.md
```

The runner prints its directory under `data/corpus_extractions/`. Before model
calls, it validates all masks and freezes `inputs.jsonl`, `prompt.md`, the ontology,
company mapping and relevant source code. It defaults to eight requests in flight;
`--workers` accepts 1–32. `results/000001.json` and subsequent files retain each chunk's provenance,
exact request, raw response and validation result or rejection. Raw responses
are saved before validation. `manifest.json` tracks counts and API-reported usage;
progress is also printed about every 20 seconds or 25 completions.

A rejected model output does not stop the corpus. Operational errors are recorded
separately; eight consecutive operational failures stop new dispatch while active
requests finish. There are no automatic paid retries. `complete_with_errors`
means the pass finished but some outcomes were rejected, operational failures or
uncertain; it does not mean every chunk yielded accepted assertions.

To continue a stopped run, optionally increasing concurrency:

```sh
.venv/bin/python -u -m picton_graph.extraction.corpus --workers 32 \
  --resume "data/corpus_extractions/<run-id>"
```

Resume verifies frozen file hashes, code/configuration, and result-to-request
bindings before new calls. Worker changes are recorded; the model, prompt and token
budget must stay the same. It processes only unstarted chunks. Existing valid,
rejected and operational-error outcomes are retained. A saved response can be
validated locally; a request intent with no saved response becomes `uncertain`
and is not silently sent again. A per-run lock prevents simultaneous resumes.
Atomic checkpoints support ordinary process restarts; they are not an exactly-once
billing guarantee across host or power failures.

New runs use manifest `run_version: 2` with package-relative source snapshots.
The repository reorganization does not rewrite historical version-1 manifests,
responses, prompts, or saved code. Those frozen runs require their archived
original runtime to resume or produce a new de-anonymized export; current code
intentionally rejects their runtime fingerprints. Existing completed
`deanonymized/` exports remain readable and importable with the current importer.

This workflow writes local review candidates, not Neo4j assertions. The original
DuckDB database and HTML stay unchanged. For a long run, keep the host awake and
connected; a detached process can continue after the launching shell exits.

Offline runner checks use a mocked provider:

```sh
.venv/bin/python -m unittest -v tests.test_run_corpus_extraction
```

## Restore company names for graph import

```sh
.venv/bin/python -m picton_graph.extraction.export \
  "data/corpus_extractions/<completed-v2-run-id>"
```

This creates `deanonymized/assertions.jsonl` and a manifest inside the completed
run directory. Each line contains one validated chunk's `nodes` and
`relationships`, plus source provenance. Rejected outputs are excluded. Existing
exports are never overwritten; the original extraction files remain unchanged.

Company names, tickers and identifiers come from the run's frozen mapping.
Product, service, topic and event names containing company labels are restored as
well. Evidence comes from the exact original filing quote, preserving the aliases
and legal suffixes used by that source. Supply annotations reference the exported
Product and Service identifiers.

Node and relationship `properties` follow the current Neo4j schema. Literal time
qualifiers, filing dates and evidence offsets remain alongside those properties
as provenance; no precise event dates are invented. Company and document nodes
repeat across records and should be merged by `iri` during import. Other entities
retain their existing chunk-specific identities, and separate supporting
assertions are preserved. Only the nine canonical edge types are exported.

The export revalidates saved responses locally and makes no LLM calls or Neo4j
writes. Passing validation does not establish semantic extraction accuracy.

```sh
.venv/bin/python -m unittest -v tests.test_export_deanonymized
```
