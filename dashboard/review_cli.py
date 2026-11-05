"""Command line access to the review workflow, using the same rules as the dashboard.

  python review_cli.py list [--status pending]
  python review_cli.py decide REVIEW_ID approved|rejected|resolved --actor NAME [--note TEXT]

decide loads the item's current status and version and passes them as the expected values,
so it is refused if the item is not in a state that allows the transition.
"""
import argparse
import json
import os
import sys
from contextlib import closing

import psycopg2

import reviews


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--status")
    decide = sub.add_parser("decide")
    decide.add_argument("review_id", type=int)
    decide.add_argument("to_status", choices=["approved", "rejected", "resolved"])
    decide.add_argument("--actor", required=True)
    decide.add_argument("--note")
    args = parser.parse_args()

    with closing(psycopg2.connect(os.environ["DATABASE_URL"])) as conn:
        if args.command == "list":
            with conn.cursor() as cur:
                cur.execute("""SELECT id, status, version, case_ref, findings ->> 'verdict', reason FROM review_items
                               WHERE %(s)s::text IS NULL OR status = %(s)s ORDER BY id""", {"s": args.status})
                for row in cur.fetchall():
                    print(json.dumps(dict(zip(("id", "status", "version", "case_ref", "verdict", "reason"), row))))
            return 0
        current = reviews.get(conn, args.review_id)
        if current is None:
            print(f"review {args.review_id} does not exist", file=sys.stderr)
            return 1
        try:
            item = reviews.transition(conn, args.review_id, to_status=args.to_status, actor=args.actor,
                                      expected_status=current.status, expected_version=current.version,
                                      note=args.note)
        except reviews.ReviewError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps({"id": item.id, "status": item.status, "version": item.version}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
