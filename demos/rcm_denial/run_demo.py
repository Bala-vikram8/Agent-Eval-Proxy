"""Runs the three synthetic denial investigations and reports what the evaluator found.

  CLM-24-10031  authorization denial, evidence supports a corrected claim
  CLM-24-10047  timely filing denial, the agent applies a retired policy and invents an amount
  CLM-24-10052  medical necessity denial, the payer policy is missing from the library

When DATABASE_URL is set it waits for the grounded evaluations and prints each verdict and
any review item opened. It only reads from the database.
"""
import os
import sys
import time

import agent

SCENARIOS = ["CLM-24-10031", "CLM-24-10047", "CLM-24-10052"]


def wait_for_results(dsn: str, trace_ids: list[str], timeout: float = 90) -> list[tuple]:
    import psycopg2

    query = """
        SELECT t.case_ref, t.trace_id, e.status, e.verdict, e.needs_review, r.id, r.status,
               e.result -> 'reasons'
        FROM traces t
        JOIN trace_evaluations e ON e.trace_id = t.id AND e.eval_type = 'grounded'
        LEFT JOIN review_items r ON r.evaluation_id = e.id
        WHERE t.trace_id = ANY(%s) AND e.status IN ('completed', 'failed')
        ORDER BY t.id"""
    deadline = time.time() + timeout
    rows = []
    while time.time() < deadline:
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(query, (trace_ids,))
            rows = cur.fetchall()
        if len(rows) == len(trace_ids):
            return rows
        time.sleep(1)
    return rows


def main() -> int:
    print(f"Synthetic denial investigations through {agent.PROXY_URL} with model {agent.MODEL}\n")
    final_traces = []
    for claim_id in SCENARIOS:
        state = agent.investigate(claim_id)
        final_traces.append(state["trace_ids"][-1])
        print(f"{claim_id}\n  recommendation: {state['recommendation']}\n"
              f"  model calls: {len(state['trace_ids'])}, final trace: {state['trace_ids'][-1]}\n")

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("Set DATABASE_URL to wait for evaluation results, or open the dashboard.")
        return 0
    print("Waiting for grounded evaluations of the final recommendations...")
    rows = wait_for_results(dsn, final_traces)
    for case_ref, trace_id, status, verdict, needs_review, review_id, review_status, reasons in rows:
        review = f"review #{review_id} {review_status}" if review_id else "no review needed"
        print(f"  {case_ref}: {status}, verdict={verdict}, {review}")
        for reason in reasons or []:
            print(f"      - {reason}")
    if len(rows) != len(final_traces):
        print(f"Only {len(rows)} of {len(final_traces)} evaluations finished in time.")
        return 1
    print("\nThe original recommendations already reached the agent; review is after the fact.")
    print("Dashboard: http://localhost:8501")
    return 0


if __name__ == "__main__":
    sys.exit(main())
