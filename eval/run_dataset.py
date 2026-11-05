"""Measure the grounded evaluator against the labeled synthetic dataset.

  python eval/run_dataset.py --judge deterministic        identifier check only, no LLM
  python eval/run_dataset.py --judge mock --base-url URL  mock upstream heuristic stub
  python eval/run_dataset.py --judge openai               real LLM judge (needs OPENAI_API_KEY
                                                          or JUDGE_API_KEY/JUDGE_BASE_URL)

Uses the same grounded.evaluate() the worker uses. Writes a JSON report with the judge mode
and model in it, so results from the stub can never be mistaken for model accuracy.
Real judge runs are not deterministic across model versions; record the date and model.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grounded  # noqa: E402

DEFAULT_DATASET = Path(__file__).resolve().parent / "datasets" / "grounded_rcm_v1.json"
REVIEW_VERDICTS = {"unsupported", "contradicted", "insufficient_evidence", "incomplete"}


def make_judge(mode: str, base_url: str | None, model: str):
    if mode == "deterministic":
        return None
    from openai import OpenAI
    if mode == "mock":
        client = OpenAI(api_key="mock", base_url=base_url, max_retries=0, timeout=30)
    else:
        key = os.getenv("JUDGE_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not key:
            raise SystemExit("--judge openai needs OPENAI_API_KEY or JUDGE_API_KEY")
        client = OpenAI(api_key=key, base_url=base_url or os.getenv("JUDGE_BASE_URL") or None,
                        max_retries=2, timeout=60)

    def call(messages):
        result = client.chat.completions.create(model=model, messages=messages, temperature=0, max_tokens=1200,
                                                response_format={"type": "json_object"})
        usage = result.usage
        return result.choices[0].message.content, {
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None}
    return call


def run(dataset_path: Path, mode: str, base_url: str | None = None, model: str = "gpt-4o-mini") -> dict:
    dataset = json.loads(dataset_path.read_text())
    judge = make_judge(mode, base_url, model)
    rows = []
    for case in dataset["cases"]:
        acceptable = case.get("acceptable_verdicts") or [case["expected_verdict"]]
        try:
            result = grounded.evaluate(case, judge, REVIEW_VERDICTS)
            predicted, review, error = result["verdict"], result["needs_review"], None
            reasons = result["reasons"]
        except Exception as exc:  # noqa: BLE001  an evaluation that could not complete is reported, not hidden
            predicted, review, error, reasons = "evaluation_failed", None, f"{type(exc).__name__}: {exc}", []
        rows.append({"id": case["id"], "category": case["category"], "expected": acceptable,
                     "predicted": predicted, "correct": predicted in acceptable,
                     "expected_review": case.get("expected_needs_review"), "predicted_review": review,
                     "reasons": reasons, "error": error})

    labeled = [r for r in rows if r["category"] != "ambiguous"]
    review_rows = [r for r in rows if r["expected_review"] is not None and r["predicted_review"] is not None]
    false_negatives = [r["id"] for r in review_rows if r["expected_review"] and not r["predicted_review"]]
    false_positives = [r["id"] for r in review_rows if not r["expected_review"] and r["predicted_review"]]
    should_review = sum(1 for r in review_rows if r["expected_review"])
    flagged = sum(1 for r in review_rows if r["predicted_review"])
    confusion = Counter((r["expected"][0], r["predicted"]) for r in labeled)
    return {
        "dataset": dataset["name"], "cases": len(rows), "judge_mode": mode,
        "judge_model": None if mode == "deterministic" else (model if mode == "openai" else "mock heuristic stub"),
        "rubric_version": grounded.RUBRIC_VERSION, "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verdict_accuracy_unambiguous": f"{sum(r['correct'] for r in labeled)}/{len(labeled)}",
        "ambiguous_within_acceptable": f"{sum(r['correct'] for r in rows if r['category'] == 'ambiguous')}/"
                                       f"{sum(1 for r in rows if r['category'] == 'ambiguous')}",
        "review_routing": {
            "should_review": should_review, "flagged": flagged,
            "false_negatives": false_negatives, "false_positives": false_positives,
            "recall": round((should_review - len(false_negatives)) / should_review, 3) if should_review else None,
        },
        "evaluation_failures": [r["id"] for r in rows if r["error"]],
        "confusion": [{"expected": e, "predicted": p, "count": n} for (e, p), n in sorted(confusion.items())],
        "cases_detail": rows,
    }


def print_report(report: dict) -> None:
    print(f"dataset {report['dataset']} ({report['cases']} cases), judge: {report['judge_mode']}"
          f" [{report['judge_model'] or 'no LLM'}], rubric {report['rubric_version']}")
    print(f"verdict accuracy, unambiguous cases: {report['verdict_accuracy_unambiguous']}")
    print(f"ambiguous cases within an acceptable verdict: {report['ambiguous_within_acceptable']}")
    rr = report["review_routing"]
    print(f"review routing: recall {rr['recall']}, false negatives {rr['false_negatives']}, "
          f"false positives {rr['false_positives']}")
    if report["evaluation_failures"]:
        print(f"evaluations that could not complete: {report['evaluation_failures']}")
    print("\nid   category               expected                      predicted              ok")
    for r in report["cases_detail"]:
        print(f"{r['id']:<4} {r['category']:<22} {'/'.join(r['expected']):<29} {r['predicted']:<22} "
              f"{'yes' if r['correct'] else 'NO'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge", choices=["deterministic", "mock", "openai"], default="deterministic")
    parser.add_argument("--base-url")
    parser.add_argument("--model", default=os.getenv("JUDGE_MODEL", "gpt-4o-mini"))
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = run(args.dataset, args.judge, args.base_url, args.model)
    print_report(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2))
        print(f"\nreport written to {args.out}")


if __name__ == "__main__":
    main()
