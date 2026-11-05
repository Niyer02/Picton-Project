# Extraction gold set, v1.1 draft

Twenty deliberately difficult examples selected from the existing SEC corpus,
with expected JSON authored by reading the source passages. These are
**assistant-curated draft labels, independently reviewed by assistant reviewers**;
they have not been approved by a human or produced by a run of the extractor.
No OpenRouter calls, GEPA runs, graph writes, or source-data changes were used to
create the labels. Subsequent optimization uses the separate `picton_graph.evaluation.optimize` runner
and does not rewrite these frozen examples.

Version `1.1-draft` changes only the response representation to
`named-entities-v1`: entities have no generated IDs, relationship endpoints are
typed names, and supplied-item references are exact entity names. All facts,
evidence, inputs, annotations, provenance and split membership are unchanged.
The completed GEPA run `f39fbdba-45c3-4915-adec-a0c1a7a91c51` used version
`1.0-draft` with local entity IDs. Its requests, scores and other historical
artifacts remain unchanged; they are not results for the new output format.
Its `gold_snapshot/` directory preserves the exact six pre-migration gold files,
including the original manifest, for reproducibility.

Start with [review.md](review.md). It shows every masked input, expected entity,
expected edge, rejected inference, source link and labeling rationale. The JSONL
files contain the full expected response, including verbatim evidence quotes.

## Files and splits

| File | Examples | Filing issuers | Intended use |
|---|---:|---|---|
| `train.jsonl` | 12 | NVDA, AMD, MU, AMAT, KLAC, LRCX | GEPA training and reflection feedback |
| `validation.jsonl` | 4 | INTC, AVGO | Candidate selection during optimization |
| `holdout.jsonl` | 4 | TSM, ASML | Evaluation only after the prompt is frozen |

Every filing issuer belongs to exactly one split. This also keeps each document
and its excerpts together, instead of scattering nearly identical issuer
boilerplate between training and evaluation. Different issuers can still discuss
the same companies or business facts; this is not a guarantee of statistical
independence. No exact input text is duplicated across the splits.

The training examples cover all nine canonical relationship types and all six
model-created entity types. The seventh type, `SourceDocument`, is supplied by
the existing `{"type":"SourceDocument","name":"document"}` endpoint. The complete set covers all ten filing issuers,
10-K, 10-Q, 20-F, 8-K and 6-K forms, primary documents and exhibits, prose and a
financial table. Three examples require completely empty outputs.

`manifest.json` records the ontology hash, starting prompt hash, split counts,
coverage and current dataset file hashes. `previous_format_files_sha256` records
the hashes of all six files before migration. The ontology hash and
`prompt_sha256_at_creation` remain the original historical values. Relabel
affected examples if the ontology changes. Optimizing the prompt itself need
not invalidate the dataset.

## Unit of annotation and provenance

Each example is an **exact contiguous excerpt from an existing masked chunk**.
It is not a synthetic rewrite and usually is not the entire 6,000-character
chunk. Paragraphs or complete sentences were selected to isolate a difficult
decision while retaining the context needed to label it. The excerpt length is
therefore not representative of production chunk length.

Each JSONL record has:

- `input`: exactly the model-facing context object used by the extractor,
  including masked text/heading, masked issuer, document role/type and dates.
  `contains_table` describes the selected excerpt; the parent chunk's flag is
  retained separately in provenance.
- `expected_output`: an example-specific canonical JSON response matching the
  extractor's `named-entities-v1` contract, with quotes and supplied-item bindings.
  Entity objects retain `type`, `name`, `evidence_text`, `event_type` and
  `time_text`, without an `id`. Each relationship's `source` and `target` is a
  `{"type": ..., "name": ...}` object. `supplied_product` and `supplied_service`
  contain exact Product and Service names, respectively. The input still uses
  `document_ref: "document"`; the existing source endpoint is represented in
  output as `{"type":"SourceDocument","name":"document"}`.
- `annotation`: challenge tags, selection rationale, specific forbidden
  inferences and adjudication notes suitable for later evaluator feedback.
- `provenance`: local document/chunk identifiers, SEC URL, source/parent hashes,
  exact masked and original character intervals, original excerpt and rebased
  mask spans. Offsets are zero-based Python character positions, end-exclusive.

Only `input` belongs in an extraction request. Do not send the whole record,
source URL, original text, ticker, labels, or annotation notes to the extraction
model. For GEPA reflection on training examples, the evaluator may provide the
input, expected facts and feedback; keep unmasked provenance out of that feedback.
Never include holdout examples or labels in optimization or reflection prompts.

## Labeling decisions

The labels preserve the current seven-class/nine-relationship ontology:

1. Only the ten masked companies can become Company nodes. Customer A is an
   anonymous customer identifier, not Company A. Outside-basket counterparties
   may be described in an Event but do not become extra Company nodes.
2. Supplier/customer direction needs a direct business relationship. Competition,
   collaboration, hiring, legal disputes, and purchases of securities do not
   establish supply. A competitors list headed “suppliers” is still a competitors
   list. Products are attached only to the supported supplier/customer pair.
3. Manufacturing work is a Service; its physical wafer output is a Product.
   Software licenses and securities are not physical Products in this ontology.
   The fact a filing uses the everyday word “product” does not change that scope.
4. A specific actual occurrence or announcement with evidenced time can be an
   Event. Agreements and completed transactions are separate occurrences.
   Expectations, intended uses of funds and risk scenarios are not completed
   events. A periodic accounting snapshot alone is not a discrete Event in v1;
   an EPS decline does not establish an earnings miss without expectations.
5. Primary-filing issuer narration can resolve we/our. Explicitly attributed
   quotations override the issuer. Exhibit filing metadata alone does not
   identify an otherwise unbound speaker.
6. Source-document mentions are substantive references. Navigation, page headers,
   and a table of contents do not establish such references. Topics must be
   meaningful literal source phrases, not invented summaries of financial ratios.
7. Unknown products, customers and dates stay unknown. No filing-date substitution,
   inferred EPS causality, transitive supply edges, or duplicate inverses.

Some labels involve reasonable semantic choices, especially Topic granularity,
manufacturing-service names and how to group physical product families. Those
choices are recorded per example. Review them before freezing this draft; do not
quietly change the ontology just to make a difficult example easier.

## Use with GEPA

GEPA accepts training and validation datasets through an adapter/evaluator and
can use textual feedback to revise a prompt. Its validation set participates in
candidate selection, so it is not our untouched final test set.
[Official GEPA API](https://gepa-ai.github.io/gepa/api/core/optimize/)

`picton_graph.evaluation.optimize` and `picton_graph.evaluation.scoring` now provide the bounded optimizer and a
deterministic canonical-agreement evaluator. The following principles remain
important when interpreting that proxy score and manually reviewing results:

- Validates schema, exact quotes, node types and references with the existing
  extractor checks. Invalid structure and unsupported evidence are failures.
- Compares entity/edge meaning using typed names. Array order and the orientation
  of symmetric competition are not substantive errors. Type remains part of
  identity when a Sector and Topic share the same source phrase.
- Accepts different exact quote boundaries when the quote still establishes the
  complete fact. Do not use byte-for-byte JSON equality as the reward.
- Treats event name wording and source-supported entity aliases with adjudicated
  equivalence. Distinct occurrences and time qualifiers must remain distinct.
- Uses the canonical targets as positive labels and `must_not_extract` as explicit
  negative guidance. An additional supported fact is not automatically a false
  positive: review it and freeze accepted alternatives before scoring. Topic
  associations are especially prone to multiple valid outputs.
- Reports substantive business/event edges separately from documentary mentions;
  many easy mentions must not conceal incorrect supplier direction or invented
  relationships. Provide concrete feedback about the failed evidence or direction.

These twenty examples are a targeted challenge set, not a random sample. They
cannot establish corpus-wide error rates or guarantee that optimization improves
performance on full production chunks. Four holdout examples provide a smoke
test, not a statistically reliable accuracy estimate. Add a later random sample
of complete chunks for the take-home's reported extraction spot-check.

## Offline verification

Run from the repository root after the editable research install described in
the [setup guide](../../docs/setup.md).

```sh
.venv/bin/python -m unittest -v tests.test_gold_set
```

The tests validate every target with the existing extractor, check split/coverage
invariants and dataset hashes, and verify each excerpt against the read-only local
DuckDB source. They make no model calls. Human semantic approval remains pending.
