# Gold labels and prompt evaluation

[Project overview](../README.md)

All shell commands below run from the project root, after the editable install
described in the [setup guide](setup.md). Paths in prose are relative to that root.

## Draft gold examples for prompt optimization

`benchmarks/gold/` contains 20 annotated, exact source excerpts covering the ten filing
companies and all nine relationship types. Start with `benchmarks/gold/review.md` for
the readable inputs, expected facts and rejected inferences. The JSONL files
contain canonical extraction responses with exact evidence and source offsets.

The split is 12 training, four validation and four holdout examples, grouped by
filing issuer. These are assistant-curated and independently assistant-reviewed
draft labels; human approval remains pending. They are a targeted challenge set,
not a random accuracy sample. `benchmarks/gold/README.md` documents annotation choices,
GEPA integration boundaries and scoring considerations.

```sh
.venv/bin/python -m unittest -v tests.test_gold_set
```

## GEPA prompt optimization

The latest three-epoch named-reference rerun is documented in
[Named-reference GEPA report](reports/gepa_named_references.md), including comparison with the first run,
remaining errors, held-out review and actual API usage.

The first three-epoch experiment is documented in [Initial GEPA report](reports/gepa_initial.md),
including unchanged validation performance, held-out review, usage and limitations.
That historical run used the previous ID-based output format. Its original gold
files and code are preserved under its `gold_snapshot/` and `source_snapshot/`.
The current gold set is version `1.1-draft`: only the response representation was
migrated to named references, with the same facts and splits. The rerun verified
the new format through OpenRouter: ID/reference failures fell to zero, but GEPA
did not beat the revised starting prompt. The four holdout cases were reused;
these are repeated benchmark results, not a fresh unseen test.

`picton_graph/evaluation/optimize.py` uses pinned GEPA 0.1.4 and the existing OpenRouter transport. It uses
`openai/gpt-6-luna` for both extraction and reflection. Set `OPENROUTER_API_KEY` in
the environment or project `.env`; credentials are never written into artifacts.

```sh
.venv/bin/python -m picton_graph.evaluation.optimize --epochs 3 --workers 3
```

One epoch is one shuffled pass over the 12 training examples in batches of three.
Three epochs means 12 sampled proposal rounds. Every training example must occur
exactly once per epoch, and every round must produce a non-empty instruction;
otherwise the run is incomplete. This explicit completion check handles GEPA's
internal reflection-error handling. Validation selects the prompt. The holdout
file is loaded for evaluation only after the selected prompt is saved, and cannot
enter reflection. The default run makes live paid model calls; unit tests mock them.

`picton_graph/evaluation/scoring.py` is a frozen deterministic **canonical agreement** metric.
The existing schema/evidence validator runs first; invalid outputs score zero.
Typed names resolve endpoints; array ordering, valid quote-boundary differences, and symmetric
competition orientation are ignored. Event identity includes normalized type,
literal occurrence time and Company participants. Conservative aliases are
defined in the scorer before inference. Active-category F1 weights are business
and event edges 50%, entities 20%, supplied-item bindings 15%, temporal qualifiers
10%, and documentary edges 5%. Unmatched facts are not automatically semantic
errors; they need source review. Completely empty gold requires empty output.

Each run is saved under `data/gepa/<run-id>/`: initial/selected prompts, frozen
source snapshots and hashes, per-call requests/responses and usage, per-example
scores and feedback, shuffled rounds/proposals, GEPA state, validation history,
and final `results.json`. Identical requests within a run are cached, so repeated
evaluation does not bill again. There is one response per unique request, not a
multi-seed reliability estimate. Operational API failures stop the workflow;
they are not scored as extraction mistakes. An incomplete run preserves its logs.

The original `prompts/extraction.md`, gold labels, DuckDB and Neo4j are not replaced
by optimization. `optimized_prompt.md` is the selected candidate for review; GEPA
can select the original prompt if none performs better on validation. Rerunning
the command creates a new run and incurs new calls.

```sh
.venv/bin/python -m unittest -v tests.test_run_gepa tests.test_score_extraction
```
