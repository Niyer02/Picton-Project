# GEPA rerun with named references — 4 November 2026

**Three epochs / 12 proposal rounds completed. Entity ID-format failures fell to zero, but no GEPA revision beat the new starting prompt on validation.** GEPA selected candidate 0, the prompt with the ID fix already applied. The selected prompt and baseline are identical.

## Comparison

These scores measure canonical agreement with draft labels, not extraction accuracy.

| Split | Examples | Previous run, selected | New starting prompt | New GEPA-selected prompt |
|---|---:|---:|---:|---:|
| Training | 12 | 35.69% | 56.81% | 56.81% |
| Validation | 4 | 83.60% | 89.34% | 89.34% |
| Reused holdout | 4 | 42.26% | 50.17% | 50.17% |

The new baseline improved by 21.12, 5.74 and 7.92 percentage points respectively. **GEPA added no measured improvement over that baseline.** Identical requests are cached within each run, so the equal baseline/selected results share responses rather than representing independent repetitions.

The model, three-epoch budget, batch size, split membership, labeled facts, scoring weights and aliases were retained. The response representation and corresponding prompt examples changed from temporary IDs to typed names. Responses were newly sampled; this single comparison cannot attribute every score change solely to the ID fix.

## Output validation

| Check | Previous run | Named-reference rerun |
|---|---:|---:|
| All unique extraction evaluations, including rejected candidates | 83 | 103 |
| Outputs rejected by validator | 25 | 5 |
| Entity ID-format failures | 21 | **0** |
| Selected prompt: valid outputs across the 20 examples | 19/20 | **20/20** |

The five remaining rejections were three non-verbatim evidence quotes, one relationship period absent from its own quote, and one duplicate assertion. For example, a quote changed the source's capitalized “We” to “we.” Exact-evidence checks were preserved. Candidate totals cover different generated prompts, so their failure rates are diagnostic rather than a controlled accuracy comparison.

## Optimization history

Seven revised prompts improved their sampled training batch enough to receive full validation; five did not. Three revisions tied the baseline and four scored lower. The best available validation score remained 89.34% after every epoch.

| Proposal round | Candidate | Validation agreement |
|---|---:|---:|
| Starting prompt | 0 | **89.34%** |
| 1 | 1 | 69.24% |
| 3 | 2 | 89.34% |
| 5 | 3 | 69.24% |
| 6 | 4 | 89.34% |
| 8 | 5 | 89.34% |
| 9 | 6 | 67.28% |
| 11 | 7 | 58.58% |

The starting prompt won the tie. The scorer was not altered to favor any response.

## Source-based review

An independent assistant reviewed all four held-out outputs against their excerpts and labels. The selected prompt produces the same outputs as the new baseline.

| Example | Previous score | New score | Review |
|---|---:|---:|---|
| gold_03: named customers | 100.00% | 100.00% | Correctly extracts all four customer links. |
| gold_06: customer quotation | 58.33% | 90.00% | Now captures Company J → Company C supply and its manufacturing-service binding. Still omits the historical qualifier on `supplies_to` and `provides`. |
| gold_14: product shipments | 10.70% | 10.70% | Captures the main product, actual 2025 shipment and company links. Omits three component-product mentions and the offering's year qualifier. Product/event naming differences additionally depress the score. |
| gold_16: table of contents | 0.00% | 0.00% | Still extracts an irrelevant Company D/document mention from navigation. |

Three of four examples contain an omission or policy violation: two omission cases and one navigation false positive. None of the eight emitted business edges was judged fabricated. This is a small assistant spot-check, not human adjudication or a corpus error rate. The supplier omission in gold_06 was recovered, even though the example-level issue count stayed the same because missing time qualifiers remain.

The only baseline validation deduction is gold_10. It correctly captures the agreement and completed share transaction, their dates and both companies. Its `share_issuance_completion` label and the gold's `equity_sale_completed` label describe the same evidenced occurrence. The frozen scorer does not recognize that synonym and penalizes the event and connected edges. Its 57.35% example score therefore overstates the semantic problem. No post-hoc synonym was added.

## Execution and evidence

- GEPA 0.1.4; `openai/gpt-6-luna` for both extraction and reflection; seed 0; batches of three; three concurrent extraction requests.
- Every training example appeared once in each epoch; all 12 reflections produced usable proposals.
- **115 live OpenRouter requests:** 103 extraction and 12 reflection.
- API-reported usage: **798,184 tokens** (620,464 prompt; 177,720 completion).
- API-reported cost: **US$0.12049965**, approximately twelve cents. Every call reported cost.
- Runtime: approximately **17 minutes 12 seconds**, 18:36:47–18:53:59 America/Toronto on 4 November 2026.
- Completion audit passed: source/gold snapshots and current hashes, epoch coverage, selection, all 103 evaluation validity decisions and scores, reported model, usage, holdout timing/isolation, and absence of credentials in saved artifacts.

The four holdout cases were excluded from this run's reflection and selection. They had already been inspected after the first experiment, so this is a repeated benchmark, **not a fresh unseen test**. Twenty deliberately difficult excerpts with assistant-curated draft labels cannot establish full-corpus performance. Free-form name matching remains a limitation of the metric.

The ID-format change was already covered by 75 passing offline tests, with five live Neo4j checks skipped, before this run. No implementation, prompt, label or metric changes were made during the experiment. No full-corpus extraction or graph ingestion was performed.

## Artifacts

Run directory: `data/gepa/ca912a0b-311d-4cfd-8108-60d0fd5e0dc8/`.

- [Results](../../data/gepa/ca912a0b-311d-4cfd-8108-60d0fd5e0dc8/results.json)
- [Selected prompt](../../data/gepa/ca912a0b-311d-4cfd-8108-60d0fd5e0dc8/optimized_prompt.md)
- [Completion audit](../../data/gepa/ca912a0b-311d-4cfd-8108-60d0fd5e0dc8/completion_audit.json)
- [Manifest](../../data/gepa/ca912a0b-311d-4cfd-8108-60d0fd5e0dc8/manifest.json)
- [Experiment context and comparison limits](../../data/gepa/ca912a0b-311d-4cfd-8108-60d0fd5e0dc8/experiment_context.json)
- [Previous run report](gepa_initial.md)

The run also retains exact requests/responses, per-example predictions and feedback, proposal history, optimizer state, and frozen source/gold snapshots.
