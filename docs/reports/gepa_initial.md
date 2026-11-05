# GEPA extraction experiment — 4 November 2026

**Three epochs completed. No revised prompt beat the original prompt on validation, so GEPA retained the original.** The selected prompt is byte-for-byte identical to this run's `baseline_prompt.md`. This run provides no evidence of an extraction improvement.

This is the historical experiment with the ID-based output format and gold set
`1.0-draft`. The current extractor subsequently moved to named references and gold
set `1.1-draft`, preserving the labeled facts. The subsequent provider experiment
is documented separately in [Named-reference rerun](gepa_named_references.md).
Original gold files are retained in this run's `gold_snapshot/`;
the results below have not been changed or rescored.

## Setup

- GEPA 0.1.4; `openai/gpt-6-luna` through OpenRouter for extraction and reflection.
- Frozen draft gold set: 12 training, four validation, four holdout excerpts, grouped by filing issuer.
- One epoch: a shuffled pass over all 12 training examples in batches of three. Three epochs produced 12 successful prompt proposals; each training example appeared exactly once per epoch.
- Validation selected the prompt. Holdout inference started only after the selected prompt was saved. Holdout text never entered reflection.
- Seed 0, three concurrent extraction requests, no merge proposals. Identical requests were cached within the run. Scores reflect one response per unique request, not repeated sampling.

## Results

Scores are **canonical agreement with draft labels, not semantic accuracy**.

| Split | Examples | Original | Selected | Schema/evidence-valid outputs |
|---|---:|---:|---:|---:|
| Training | 12 | 35.69% | 35.69% | 11/12 |
| Validation | 4 | 83.60% | 83.60% | 4/4 |
| Holdout | 4 | 42.26% | 42.26% | 4/4 |

The selected and original prompts share the same cached responses because they are identical. Their equal results are not independent repeat measurements. Passing schema/evidence checks does not establish that the model interpreted a quote correctly.

Five of the 12 revised prompts improved their sampled training batch enough to receive full validation:

| Proposal round | Candidate | Validation agreement |
|---|---:|---:|
| Original | 0 | **83.60%** |
| 1 | 1 | 58.58% |
| 2 | 2 | 4.90% |
| 6 | 3 | 66.54% |
| 10 | 4 | 79.20% |
| 11 | 5 | 0.00% |

The best available validation score remained 83.60% after each epoch. The other seven proposals were rejected at the training-batch comparison.

## Manual holdout review

An independent assistant reviewer compared all four held-out outputs with their source excerpts and draft labels. This is an assistant spot-check, not human adjudication.

| Example | Agreement | Source-based assessment |
|---|---:|---|
| gold_03: named customers | 100.00% | Correctly extracted all four Company J customer relationships, with the right direction and no invented supplied items. |
| gold_06: attributed customer quotation | 58.33% | Missed Company J → Company C `supplies_to` and its manufacturing-service binding. Also omitted the historical qualifier on the service relationship. |
| gold_14: product shipments | 10.70% | Captured the main offered product and actual 2025 shipment. Different product wording and event-type labels heavily reduce the proxy score. Omitted three component-product mentions required by the draft labels and the offering's 2025 qualifier. Component granularity is a policy choice. |
| gold_16: table of contents | 0.00% | Extracted a company/document mention from navigation, which the substantive-content rule excludes. This is an irrelevant extraction, not an invented business relationship. |

**Three of four examples have a coverage or policy issue:** two contain omissions and one contains a navigation false positive. No clearly fabricated business relationships were found in these four outputs. This is not a relationship-level error rate or a corpus-wide estimate.

## What the experiment exposed

Prompt revisions frequently lost output-contract rules. Across 83 unique extraction evaluations, 25 failed validation: 21 had invalid local entity IDs, two had non-verbatim quotes, one had an invalid supplied-item reference, and one had an invalid `has_topic` target. Those outputs scored zero. These counts include rejected candidates, not just the selected prompt.

The original prompt also has real weaknesses. Its gold_07 response added a period to an extracted clause that continued with a comma in the source, breaking exact-quote validation. In gold_20 it classified software as a physical Product, contrary to this ontology.

The metric also penalizes legitimate alternatives. For example, `public_offering_completion` versus the gold's debt-issuance label can describe the same occurrence. Event-type differences affect the event and its incident edges, so their score impact is large. Entity-name granularity and optional topic associations create similar mismatches. No labels, aliases, weights, or scoring rules were changed after inference began.

Before another optimization experiment, preserve invariant output rules outside the editable prompt, review the draft labeling policy, and agree on event-type/name equivalence. A subsequent assessment should use fresh held-out examples and include complete production-sized chunks. The current holdout has now been inspected.

## Scoring and limits

The existing validator checks shape, ontology types, references, literal names and exact source quotes. Invalid outputs score zero. Valid outputs receive weighted F1 over active categories: business/event edges 50%, entities 20%, supplied-item bindings 15%, temporal qualifiers 10%, and documentary edges 5%. Local IDs, array order, valid quote boundaries and symmetric competition direction are ignored. A completely empty target requires empty output.

The 20 labels are assistant-curated, independently assistant-reviewed drafts; human review is pending. They are deliberately difficult excerpts, not a random sample or full production chunks. Four holdout examples cannot establish generalization, and one provider response per request does not measure stochastic variability. An unmatched fact is not automatically false.

## Execution and verification

- 95 live requests: 83 extraction and 12 reflection. All responses identified the requested model.
- API-reported usage: 515,778 prompt tokens, 147,070 completion tokens, **662,848 total tokens**.
- API-reported total cost: **US$0.10384248**, approximately ten cents; every call reported a cost.
- Execution: 23:13:02–23:27:14 UTC, approximately 14 minutes 12 seconds.
- Implementation tests at the time of this run: **70 passed, five live Neo4j checks skipped** (75 discovered).
- Completion audit verified all 12 proposals, epoch coverage, prompt selection, holdout timing/isolation, unchanged gold/scorer hashes, reproducible stored scores, source snapshots, and absence of the API key from artifacts.

The live process used the saved source snapshot. During execution, the workspace runner gained an explicit guard against GEPA silently skipping a failed reflection. The completed live run was independently checked against that condition: all 12 reflections succeeded. Scoring and selection behavior were unchanged.

Original HTML, DuckDB chunks/masks, gold labels, the default extraction prompt and Neo4j were not modified by optimization. No full-corpus extraction or graph ingestion was performed.

## Artifacts

Run directory: `data/gepa/f39fbdba-45c3-4915-adec-a0c1a7a91c51/`.

- [Machine-readable results](../../data/gepa/f39fbdba-45c3-4915-adec-a0c1a7a91c51/results.json)
- [Selected prompt](../../data/gepa/f39fbdba-45c3-4915-adec-a0c1a7a91c51/optimized_prompt.md)
- [Completion audit](../../data/gepa/f39fbdba-45c3-4915-adec-a0c1a7a91c51/completion_audit.json)
- [Manifest and source hashes](../../data/gepa/f39fbdba-45c3-4915-adec-a0c1a7a91c51/manifest.json)
- [Validation history](../../data/gepa/f39fbdba-45c3-4915-adec-a0c1a7a91c51/validation_history.json)

The same directory contains exact requests/responses, per-example predictions and feedback, all proposed prompt text in `gepa.log`, optimizer state, and the source snapshot. The current reusable runner is `python -m picton_graph.evaluation.optimize`; rerunning it creates a separate paid experiment.
