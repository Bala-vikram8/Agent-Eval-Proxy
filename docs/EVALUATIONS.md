# Evaluation methodology

Evaluations are asynchronous. Responses are not blocked on quality scores. Each result records evaluator/version, immutable config hash/snapshot, status, score when justified and method/limitations. Relevance also records judge model, prompt version and token usage.

| Evaluator | What the result can establish | Limit |
|---|---|---|
| `answer_relevance` | Judge opinion about addressing a request | Not truth, safety, grounding or injection immunity |
| `reference_exact` | Normalized agreement with an operator-trusted answer | Does not verify the reference itself |
| `groundedness_lexical` | Lexical evidence coverage indicator | Not entailment or comprehensive hallucination detection |
| `tool_schema` | Tool name/arguments meet an allowlist/schema | Not proof the tool was appropriate for the task |
| `structured_output` | Supported bounded JSON schema validates | Not semantic correctness |
| `policy_rules` | Supported explicit literal rules match | Not complete policy/legal compliance |
| `task_completion` | Recorded task result matches an operator expectation | Cannot verify unobserved external actions |
| `execution` | Recorded API/completion outcome succeeded | Not answer quality |
| `guardrails` | Available latency/token/cost metrics meet configured limits | Unknown prices/usage remain unknown; not a prepaid spend cap |

Missing references, untrusted context, unsupported schema features and unavailable metrics produce skipped/insufficient-evidence outcomes. Missing evaluations are not passes. Content minimization may intentionally prevent content checks.

## Operator configuration

`EVAL_REFERENCE_CATALOG` is a bounded JSON map of reference IDs to answers/evidence/tool schemas/cohort labels. Callers select IDs but cannot create privileged evidence. Protect/version it as configuration; do not put real customer data or secrets into public catalogs.

`EVAL_CONFIG_FILE` selects deterministic evaluators and supported options. For example:

```json
[
  {"name":"execution","sample_rate":1},
  {"name":"reference_exact","options":{"case_sensitive":true}},
  {"name":"guardrails","options":{"max_latency_ms":5000,"max_total_tokens":4000,"max_cost_usd":0.01}}
]
```

Requests cannot replace this file. Config snapshots/version hashes and stable event/config sampling support regression comparisons. Input-size and cooperative time budgets bound deterministic checks. `JUDGE_SAMPLE_RATE`, `JUDGE_MAX_TOKENS`, `JUDGE_MAX_CHARS`, timeout and attempt limits constrain relevance calls. Per-call limits/sampling reduce expected spend, but deployment-wide budget monitoring is still necessary.

## Golden tests and calibration

`tests/fixtures/golden_evaluations.json` contains synthetic known outcomes. Tests cover wrong answers, schema violations, untrusted/missing evidence, instruction-like text, unsupported schemas, sampling, immutable hashes and malformed/non-finite metrics.

The calibration helper computes coverage, confusion counts, false positives/negatives and disagreement on supplied labels. These numbers apply only to that dataset. Synthetic disagreement tests are not calibration of a live judge. Domain owners must label representative independent tasks, review disagreements, measure error/uncertainty by cohort and choose thresholds with understood consequences. Repeat after model/prompt/reference changes.

## Candidate deterioration

The monitor compares disjoint windows matching agent/model/operator request cohort/evaluator/config. Defaults require at least 30 independent scored tasks per window, an absolute decline of 0.15 and a positive conservative lower confidence bound. Missing labels, small samples, invalid scores and changed mix are separate outcomes. Conversation turns may be correlated; aggregate/sample independent tasks before statistical interpretation. Per-comparison bounds are not family-wise guarantees across repeated monitoring.

An alert reports score deterioration to investigate, not causal model drift. Acknowledgement records investigation, not correction of the underlying behavior.
