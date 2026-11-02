"""Conservative cohort-matched quality monitoring; not proof of model drift.

The one-sided Hoeffding bound assumes independent bounded observations. Repeated
turns in the same conversation violate that assumption; operators should sample
independent tasks before relying on statistical alerts. No remote judge is called.
"""
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Iterable

RULE_VERSION = "quality-v1"


@dataclass(frozen=True)
class QualityComparison:
    status: str
    baseline_samples: int
    current_samples: int
    baseline_mean: float | None = None
    current_mean: float | None = None
    drop: float | None = None
    lower_confidence_bound: float | None = None
    message: str = ""
    rule_version: str = RULE_VERSION


def compare_quality(baseline: Iterable[float], current: Iterable[float], *,
                    baseline_cohort: tuple, current_cohort: tuple,
                    min_samples: int = 30, min_drop: float = 0.15,
                    alpha: float = 0.01) -> QualityComparison:
    """Compare disjoint windows for one agent/model/evaluator/config cohort.

    This detects observed evaluation-score deterioration. Changes in traffic,
    judge behaviour, references, or dependent samples remain alternative causes.
    Alpha is per comparison; repeated windows and multiple cohorts do not have
    a family-wise false-positive guarantee.
    """
    if type(min_samples) is not int or min_samples < 30 or not 0 < alpha < 1 or not 0 < min_drop <= 1:
        raise ValueError("Invalid monitoring configuration")
    old, new = list(baseline), list(current)
    counts = {"baseline_samples": len(old), "current_samples": len(new)}
    if baseline_cohort != current_cohort:
        return QualityComparison("request_mix_changed", **counts,
                                 message="Cohorts differ; compare matching agents, models, and evaluator versions.")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
           or not 0 <= x <= 1 for x in [*old, *new]):
        return QualityComparison("invalid_data", **counts, message="Scores must be finite values from zero to one.")
    if min(len(old), len(new)) < min_samples:
        return QualityComparison("insufficient_data", **counts,
                                 message=f"Need at least {min_samples} independent scored tasks in each window.")
    before, after = sum(old) / len(old), sum(new) / len(new)
    drop = before - after
    bound = math.sqrt(0.5 * (1 / len(old) + 1 / len(new)) * math.log(1 / alpha))
    lower = drop - bound
    detected = drop >= min_drop and lower > 0
    return QualityComparison(
        "quality_deterioration" if detected else "no_supported_deterioration", **counts,
        baseline_mean=before, current_mean=after, drop=drop, lower_confidence_bound=lower,
        message=("Evaluation scores deteriorated within a matched cohort; investigate traffic and judge changes."
                 if detected else "These samples do not support the configured deterioration threshold."),
    )


def alert_fingerprint(cohort: tuple, window_end: str, *, rule_config: dict | None = None) -> str:
    raw = json.dumps([RULE_VERSION, list(cohort), window_end, rule_config or {}],
                     sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def comparison_details(result: QualityComparison, *, cohort: tuple, window_end: str,
                       alpha: float = 0.01, min_drop: float = 0.15) -> dict:
    return {**asdict(result), "cohort": list(cohort), "window_end": window_end,
            "method": "one-sided Hoeffding bound", "alpha": alpha, "minimum_absolute_drop": min_drop,
            "limitations": "Assumes independent tasks. Alpha is per comparison, not family-wise over repeated windows/cohorts. Score deterioration does not establish model drift."}
