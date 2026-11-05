"""Human review of evaluation findings.

States and the only allowed transitions:

    pending  -> approved   reviewer accepts the agent's recommendation despite the finding
    pending  -> rejected   reviewer rejects the agent's recommendation
    approved -> resolved   follow up is complete, the item is closed
    rejected -> resolved

Every transition is one UPDATE guarded by the status and version the reviewer saw, plus an
insert into the append only review_events table, in a single transaction. If someone else
acted first (or the same click arrives twice) the guard matches no row and nothing changes.

A review happens after the fact. The agent's original response already reached its caller;
approving or rejecting here does not change or recall it.
"""
from dataclasses import dataclass

import psycopg2

TRANSITIONS = {
    ("pending", "approved"),
    ("pending", "rejected"),
    ("approved", "resolved"),
    ("rejected", "resolved"),
}
MAX_ACTOR = 80
MAX_NOTE = 2000


class ReviewError(Exception):
    """The action was refused. The message is safe to show to the reviewer."""


@dataclass
class ReviewItem:
    id: int
    status: str
    version: int


def get(conn, review_id: int) -> ReviewItem | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id, status, version FROM review_items WHERE id = %s", (review_id,))
        row = cur.fetchone()
    return ReviewItem(*row) if row else None


def transition(conn, review_id: int, *, to_status: str, actor: str, expected_status: str,
               expected_version: int, note: str | None = None) -> ReviewItem:
    actor = (actor or "").strip()
    if not actor:
        raise ReviewError("A reviewer name is required.")
    if len(actor) > MAX_ACTOR:
        raise ReviewError(f"Reviewer name is longer than {MAX_ACTOR} characters.")
    note = (note or "").strip() or None
    if note and len(note) > MAX_NOTE:
        raise ReviewError(f"Note is longer than {MAX_NOTE} characters.")
    if (expected_status, to_status) not in TRANSITIONS:
        raise ReviewError(f"A review cannot move from {expected_status} to {to_status}.")
    if to_status == "rejected" and not note:
        raise ReviewError("Rejecting a recommendation needs a note explaining why.")

    decided = to_status in ("approved", "rejected")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                f"""UPDATE review_items SET status = %s, version = version + 1, updated_at = now(),
                        {"decided_by = %s, decided_at = now(), decision_note = %s" if decided
                         else "resolved_by = %s, resolved_at = now(), decision_note = coalesce(%s, decision_note)"}
                    WHERE id = %s AND status = %s AND version = %s
                    RETURNING id, status, version""",
                (to_status, actor, note, review_id, expected_status, expected_version),
            )
            row = cur.fetchone()
            if row is None:
                current = get(conn, review_id)
                if current is None:
                    raise ReviewError(f"Review {review_id} does not exist.")
                raise ReviewError(f"Review {review_id} changed since it was loaded (now {current.status}, "
                                  f"version {current.version}); reload before acting.")
            cur.execute("""INSERT INTO review_events (review_id, from_status, to_status, actor, note)
                           VALUES (%s, %s, %s, %s, %s)""", (review_id, expected_status, to_status, actor, note))
    except psycopg2.Error as exc:
        raise ReviewError("The review could not be saved; nothing was changed.") from exc
    return ReviewItem(*row)


def history(conn, review_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""SELECT created_at, from_status, to_status, actor, note FROM review_events
                       WHERE review_id = %s ORDER BY id""", (review_id,))
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]
