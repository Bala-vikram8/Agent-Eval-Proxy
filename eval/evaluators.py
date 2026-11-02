"""Versioned deterministic indicators. Trusted context must come from the operator.

These checks do not establish general factual correctness, policy compliance, or
semantic entailment. Reference checks only compare with an explicitly trusted
expected value; lexical evidence coverage is deliberately a weaker indicator.
Untrusted response text, including instructions in it, is always treated as data.
No evaluator performs network access. JSON Schema supports a bounded, non-regex,
non-reference subset of Draft 2020-12; other keywords are explicitly unsupported.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from typing import Any, Iterable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

EVALUATOR_VERSION = "1.0.0"
NAMES = ("execution", "reference_exact", "groundedness_lexical", "tool_schema",
         "structured_output", "policy_rules", "task_completion", "guardrails")
SCHEMA_KEYS = {"$schema", "$id", "title", "description", "type", "enum", "const",
               "properties", "required", "additionalProperties", "items", "minItems",
               "maxItems", "uniqueItems", "minLength", "maxLength", "minimum", "maximum",
               "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value: Any) -> bool:
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class EvaluatorConfig:
    """Canonical JSON makes nested options immutable and hashes reproducible."""
    name: str
    version: str = EVALUATOR_VERSION
    sample_rate: float = 1.0
    options_json: str = "{}"
    max_prompt_chars: int = 12000
    max_tokens: int = 256
    timeout_seconds: float = 10.0

    def __post_init__(self):
        if not _number(self.sample_rate) or not 0 <= self.sample_rate <= 1:
            raise ValueError("sample_rate must be between zero and one")
        if (type(self.max_prompt_chars) is not int or self.max_prompt_chars < 1
                or type(self.max_tokens) is not int or self.max_tokens < 1
                or not _number(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("budgets must be positive")
        options = json.loads(self.options_json)
        if not isinstance(options, dict):
            raise ValueError("options must be an object")
        object.__setattr__(self, "options_json", _json(options))

    @property
    def options(self) -> dict:
        return json.loads(self.options_json)

    def snapshot(self) -> dict:
        return {"name": self.name, "version": self.version, "sample_rate": self.sample_rate,
                "options": self.options, **judge_budget(self)}

    @property
    def config_version(self) -> str:
        return hashlib.sha256(_json(self.snapshot()).encode()).hexdigest()


def judge_budget(config: EvaluatorConfig) -> dict:
    """The worker must apply max_tokens and timeout_seconds to its SDK call.

    Deterministic checks use max_prompt_chars and cooperative elapsed-time checks;
    they consume no model tokens. These limits are not a prepaid spending cap.
    """
    return {"max_prompt_chars": config.max_prompt_chars, "max_tokens": config.max_tokens,
            "timeout_seconds": config.timeout_seconds}


DEFAULT_CONFIGS = tuple(EvaluatorConfig(name) for name in NAMES)


@dataclass(frozen=True)
class EvaluationResult:
    evaluator_name: str
    evaluator_version: str
    config_version: str
    status: str
    score: float | None
    reason: str
    details_json: str

    @property
    def details(self) -> dict:
        return json.loads(self.details_json)

    def to_dict(self) -> dict:
        return {"evaluator_name": self.evaluator_name, "evaluator_version": self.evaluator_version,
                "config_version": self.config_version, "status": self.status, "score": self.score,
                "reason": self.reason, "details": self.details}


class _Skip(Exception):
    def __init__(self, reason: str, status: str = "insufficient_evidence"):
        self.reason, self.status = reason, status


class _Budget:
    def __init__(self, config: EvaluatorConfig):
        self.config = config
        self.deadline = time.monotonic() + config.timeout_seconds

    def check(self):
        if time.monotonic() > self.deadline:
            raise _Skip("evaluation timeout budget exceeded", "skipped")

    def data(self, value):
        """Bound nesting, collection size, and text before parsing or validation."""
        stack, nodes, chars = [(value, 0)], 0, 0
        while stack:
            self.check()
            item, depth = stack.pop()
            nodes += 1
            if depth > 24 or nodes > 2000:
                raise _Skip("evaluation structure budget exceeded", "skipped")
            if isinstance(item, str):
                chars += len(item)
            elif isinstance(item, dict):
                if len(item) > 1000:
                    raise _Skip("evaluation structure budget exceeded", "skipped")
                stack.extend((v, depth + 1) for pair in item.items() for v in pair)
            elif isinstance(item, list):
                if len(item) > 2000:
                    raise _Skip("evaluation structure budget exceeded", "skipped")
                stack.extend((v, depth + 1) for v in item)
            elif item is not None and type(item) not in (bool, int, float):
                raise _Skip("unsupported input value", "skipped")
            elif isinstance(item, (float, int)) and not isinstance(item, bool) and not _number(item):
                raise _Skip("non-finite input value", "skipped")
            if chars > self.config.max_prompt_chars:
                raise _Skip("evaluation character budget exceeded", "skipped")


def sampled(event_id: str, config: EvaluatorConfig) -> bool:
    """Stable across workers/retries; independent for each evaluator/config."""
    if config.sample_rate in (0, 1):
        return bool(config.sample_rate)
    digest = hashlib.sha256(f"{event_id}:{config.name}:{config.config_version}".encode()).digest()
    return int.from_bytes(digest[:8], "big") < config.sample_rate * 2**64


def _content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(p["text"] for p in value if isinstance(p, dict)
                         and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


def _message(trace: dict) -> dict:
    response = trace.get("response")
    choices = response.get("choices") if isinstance(response, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    return message if isinstance(message, dict) else {}


def _trusted(context: dict):
    if context.get("trusted") is not True:
        raise _Skip("trusted operator context was not supplied")


def _strict_json(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def _schema(schema: Any, value: Any, budget: _Budget) -> bool:
    budget.data(schema)
    budget.data(value)
    pending = [schema]
    while pending:
        budget.check()
        node = pending.pop()
        if isinstance(node, bool):
            continue
        if not isinstance(node, dict) or set(node) - SCHEMA_KEYS:
            raise _Skip("JSON Schema feature is unsupported", "skipped")
        properties = node.get("properties", {})
        if isinstance(properties, dict):
            pending.extend(properties.values())
        for keyword in ("items", "additionalProperties"):
            if keyword in node:
                pending.append(node[keyword])
    try:
        Draft202012Validator.check_schema(schema)
        valid = Draft202012Validator(schema).is_valid(value)
    except (SchemaError, TypeError, ValueError, RecursionError):
        raise _Skip("operator JSON Schema is invalid", "skipped") from None
    budget.check()
    return valid


def _normal(text: str, sensitive: bool = True) -> str:
    text = " ".join(text.split())
    return text if sensitive else text.casefold()


def _outcome(passed: bool, reason: str, metrics: dict | None = None):
    return ("passed" if passed else "failed", float(passed), reason, metrics or {})


def _evaluate(trace: dict, context: dict, config: EvaluatorConfig, budget: _Budget):
    name, options = config.name, config.options
    if name == "execution":
        status, outcome = trace.get("status_code"), trace.get("execution_status")
        if outcome in ("failed", "interrupted", "incomplete", "cancelled", "timeout", "upstream_error"):
            return _outcome(False, "execution did not complete successfully")
        if type(status) is not int:
            raise _Skip("execution status was not recorded")
        return _outcome(200 <= status < 300, "HTTP execution outcome; not an answer-quality check",
                        {"status_code": status})
    if name == "guardrails":
        checked, exceeded, unavailable = [], [], []
        for limit, metric in (("max_latency_ms", "latency_ms"), ("max_total_tokens", "total_tokens"),
                              ("max_cost_usd", "cost_usd")):
            if limit not in options:
                continue
            threshold = options[limit]
            if not _number(threshold) or threshold < 0:
                raise _Skip("guardrail threshold is invalid", "skipped")
            value = trace.get(metric)
            if not _number(value) or value < 0:
                unavailable.append(metric)
                continue
            checked.append(metric)
            if value > threshold:
                exceeded.append(metric)
        metrics = {"checked": checked, "exceeded": exceeded, "unavailable": unavailable}
        if exceeded:
            return _outcome(False, "recorded execution metric exceeded a configured limit", metrics)
        if unavailable or not checked:
            raise _Skip("configured metric or threshold is unavailable")
        return _outcome(True, "recorded execution metrics are within configured limits", metrics)
    _trusted(context)
    message = _message(trace)
    budget.data(message)
    reply = _content(message.get("content"))
    budget.data(reply)
    if name == "reference_exact":
        reference = context.get("reference_answer")
        if not isinstance(reference, str) or not reference.strip() or not reply.strip():
            raise _Skip("trusted reference or answer text is unavailable")
        budget.data(reference)
        same = _normal(reply, options.get("case_sensitive", True)) == _normal(reference, options.get("case_sensitive", True))
        return _outcome(same, "exact trusted-reference comparison after whitespace normalization; reference truth is not verified")
    if name == "groundedness_lexical":
        evidence = context.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(x, str) for x in evidence):
            raise _Skip("trusted evidence documents are unavailable")
        budget.data(evidence)
        tokens = set(re.findall(r"\b\w+\b", reply.casefold()))
        source = set(re.findall(r"\b\w+\b", " ".join(evidence).casefold()))
        if not tokens or not source:
            raise _Skip("answer or evidence has no lexical tokens")
        threshold = options.get("min_coverage", 0.8)
        if not _number(threshold) or not 0 <= threshold <= 1:
            raise _Skip("lexical coverage threshold is invalid", "skipped")
        coverage = len(tokens & source) / len(tokens)
        return ("passed" if coverage >= threshold else "failed", coverage,
                "lexical evidence coverage only; does not establish factual entailment or truth",
                {"coverage": coverage, "min_coverage": threshold, "answer_unique_tokens": len(tokens)})
    if name == "tool_schema":
        schemas = context.get("tool_schemas")
        calls = message.get("tool_calls", trace.get("tool_calls"))
        if not isinstance(schemas, dict) or not schemas or not isinstance(calls, list) or not calls:
            raise _Skip("trusted tool schemas or recorded tool calls are unavailable")
        budget.data(calls)
        for call in calls:
            budget.check()
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or call.get("type", "function") != "function":
                return _outcome(False, "recorded tool call has invalid structure")
            tool = function.get("name")
            if not isinstance(tool, str) or tool not in schemas:
                return _outcome(False, "recorded tool name is outside the configured allowlist")
            arguments = function.get("arguments")
            try:
                value = _strict_json(arguments) if isinstance(arguments, str) else arguments
            except (ValueError, RecursionError):
                return _outcome(False, "recorded tool arguments are not strict JSON")
            if not _schema(schemas[tool], value, budget):
                return _outcome(False, "recorded tool arguments violate the trusted schema")
        return _outcome(True, "tool names and arguments match trusted schemas; execution is not verified",
                        {"tool_calls_checked": len(calls)})
    if name == "structured_output":
        if "output_schema" not in context:
            raise _Skip("trusted output schema was not supplied")
        if not reply.strip():
            raise _Skip("answer text is unavailable")
        try:
            value = _strict_json(reply)
        except (ValueError, RecursionError):
            return _outcome(False, "answer is not strict JSON")
        return _outcome(_schema(context["output_schema"], value, budget),
                        "answer JSON compared with the trusted schema; field truth is not verified")
    if name == "policy_rules":
        rules = context.get("policy_rules")
        if not isinstance(rules, list) or not rules or not reply.strip():
            raise _Skip("explicit policy rules or answer text are unavailable")
        budget.data(rules)
        violations = 0
        for rule in rules:
            budget.check()
            if (not isinstance(rule, dict) or rule.get("type") not in ("required_literal", "forbidden_literal")
                    or not isinstance(rule.get("value"), str) or not rule["value"]):
                raise _Skip("policy rule is unsupported", "skipped")
            text, value = reply, rule["value"]
            if not rule.get("case_sensitive", True):
                text, value = text.casefold(), value.casefold()
            found = value in text
            violations += found if rule["type"] == "forbidden_literal" else not found
        return _outcome(not violations, "explicit literal rules only; not a general policy-compliance assessment",
                        {"rules_checked": len(rules), "violations": violations})
    if name == "task_completion":
        if "expected_result" not in context or "task_result" not in trace:
            raise _Skip("trusted expected result or recorded task result is unavailable")
        budget.data(context["expected_result"])
        budget.data(trace["task_result"])
        return _outcome(_json(context["expected_result"]) == _json(trace["task_result"]),
                        "recorded task result exactly compared with trusted expected result; external side effects are not verified")
    raise _Skip("evaluator is unsupported", "skipped")


def evaluate_trace(trace: dict, context: dict | None = None,
                   configs: Iterable[EvaluatorConfig] | None = None) -> list[EvaluationResult]:
    """Run independent indicators, retaining skip reasons and immutable config snapshots.

    `evaluation_context` is privileged configuration: the ingestion boundary must
    reject client/model-supplied context. Setting trusted=True is an assertion by
    that boundary, not authentication performed by this pure function.
    """
    context = context if context is not None else trace.get("evaluation_context", {})
    context = context if isinstance(context, dict) else {}
    results = []
    for config in DEFAULT_CONFIGS if configs is None else configs:
        metrics = {}
        try:
            if config.name not in NAMES:
                raise _Skip("evaluator is unsupported", "skipped")
            if config.version != EVALUATOR_VERSION:
                raise _Skip("evaluator version is unsupported", "skipped")
            if config.sample_rate not in (0, 1) and not trace.get("event_id"):
                raise _Skip("stable event identifier is required for sampling", "skipped")
            if not sampled(str(trace.get("event_id", "")), config):
                raise _Skip("excluded by deterministic sampling", "skipped")
            budget = _Budget(config)
            status, score, reason, metrics = _evaluate(trace, context, config, budget)
            budget.check()
        except _Skip as exc:
            status, score, reason = exc.status, None, exc.reason
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            status, score, reason = "error", None, "evaluation input could not be processed"
        details = {"metrics": metrics, "config_snapshot": config.snapshot(), "method": "deterministic"}
        results.append(EvaluationResult(config.name, config.version, config.config_version,
                                        status, score, reason[:500], _json(details)))
    return results


def calibration_report(records: Iterable[dict], threshold: float = 0.5) -> dict:
    """Confusion matrix on explicitly labeled data, plus optional judge disagreement.

    Input: {label: bool, score: number|null, judge_score?: number|null}. Null
    predictions remain unscored; they never count as a negative prediction.
    This report is descriptive and does not imply real-provider calibration.
    """
    if not _number(threshold) or not 0 <= threshold <= 1:
        raise ValueError("threshold must be between zero and one")
    counts = {"true_positive": 0, "false_positive": 0, "true_negative": 0, "false_negative": 0,
              "unscored": 0, "total": 0, "judge_comparisons": 0, "judge_disagreements": 0}
    for record in records:
        if type(record.get("label")) is not bool:
            raise ValueError("calibration requires boolean labels")
        counts["total"] += 1
        score = record.get("score")
        if score is None:
            counts["unscored"] += 1
            continue
        if not _number(score) or not 0 <= score <= 1:
            raise ValueError("calibration scores must be finite and between zero and one")
        predicted, label = score >= threshold, record["label"]
        key = ("true_" if predicted == label else "false_") + ("positive" if predicted else "negative")
        counts[key] += 1
        judge_score = record.get("judge_score")
        if judge_score is not None:
            if not _number(judge_score) or not 0 <= judge_score <= 1:
                raise ValueError("judge score is invalid")
            counts["judge_comparisons"] += 1
            counts["judge_disagreements"] += predicted != (judge_score >= threshold)
    tp, fp, tn, fn = (counts[k] for k in ("true_positive", "false_positive", "true_negative", "false_negative"))
    ratio = lambda a, b: a / b if b else None
    return {**counts, "threshold": threshold, "precision": ratio(tp, tp + fp),
            "recall": ratio(tp, tp + fn), "specificity": ratio(tn, tn + fp),
            "accuracy": ratio(tp + tn, tp + tn + fp + fn),
            "coverage": ratio(tp + tn + fp + fn, counts["total"]),
            "judge_disagreement_rate": ratio(counts["judge_disagreements"], counts["judge_comparisons"])}
