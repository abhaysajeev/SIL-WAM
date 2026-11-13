"""
One-off backfill: put stored phone numbers in canonical form and merge split threads.

    venv/bin/python -m scripts.normalize_numbers            # dry run (default): report only
    venv/bin/python -m scripts.normalize_numbers --apply    # make the changes
    venv/bin/python -m scripts.normalize_numbers --apply --log merges.json

Canonical = E.164 digits without '+', as Meta reports `from` (see app/core/phone.py).
Numbers without a country code are read in each company's Broadcast settings
`default_country` (IN when unset).

What it does
------------
1. **Phonebook contacts** — rewrites `phonebook_contacts.mobile_no` to canonical. If the
   canonical number is already in the same phonebook, the row is left alone and
   reported (two rows for one person is a decision for a person, not a script).
   Numbers the library cannot read are left unchanged and reported.
2. **Agent threads** — a customer stored as `8075571893` and as `918075571893` has two
   threads. They are merged into one: messages are moved onto the surviving thread and
   renumbered in time order (seq and rev both 1..n), and the empty thread is deleted.
   A thread with no canonical twin is simply re-keyed.

Take a database backup before --apply. Every change is written to the JSON log, with
old values, so it can be reversed by hand if needed.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import text

from app.core import phone
from app.core.database import SessionLocal


def _regions(db) -> dict[str, str]:
    rows = db.execute(text("SELECT company_id, default_country FROM broadcast_settings")).fetchall()
    return {str(r.company_id): (r.default_country or "IN") for r in rows}


def plan_contacts(db, regions) -> tuple[list, list]:
    changes, problems = [], []
    rows = db.execute(text("""
        SELECT pc.id, pc.phonebook_id, pc.mobile_no, p.company_id
          FROM phonebook_contacts pc JOIN phonebooks p ON p.id = pc.phonebook_id
    """)).fetchall()
    existing = defaultdict(set)
    for r in rows:
        existing[str(r.phonebook_id)].add(r.mobile_no)
    for r in rows:
        region = regions.get(str(r.company_id), "IN")
        canon = phone.try_normalize(r.mobile_no, region)
        if canon is None:
            problems.append({"contact_id": str(r.id), "mobile_no": r.mobile_no,
                             "reason": "unreadable — left unchanged"})
        elif canon != r.mobile_no:
            if canon in existing[str(r.phonebook_id)]:
                problems.append({"contact_id": str(r.id), "mobile_no": r.mobile_no,
                                 "reason": f"{canon} already in this phonebook — left unchanged"})
            else:
                changes.append({"contact_id": str(r.id), "old": r.mobile_no, "new": canon})
                existing[str(r.phonebook_id)].add(canon)
    return changes, problems


def plan_threads(db, regions) -> tuple[list, list]:
    """Group each company's threads by canonical number."""
    rows = db.execute(text("""
        SELECT id, company_id, mobile_no FROM agent_threads WHERE mobile_no IS NOT NULL
    """)).fetchall()
    groups = defaultdict(list)
    problems = []
    for r in rows:
        canon = phone.try_normalize(r.mobile_no, regions.get(str(r.company_id), "IN"))
        if canon is None:
            problems.append({"thread_id": str(r.id), "mobile_no": r.mobile_no,
                             "reason": "unreadable — left unchanged"})
            continue
        groups[(str(r.company_id), canon)].append(r)
    actions = []
    for (company_id, canon), threads in groups.items():
        if len(threads) == 1:
            t = threads[0]
            if t.mobile_no != canon:
                actions.append({"type": "rekey", "thread_id": str(t.id),
                                "old": t.mobile_no, "new": canon})
            continue
        # Survivor: the one already canonical, else the first.
        survivor = next((t for t in threads if t.mobile_no == canon), threads[0])
        actions.append({"type": "merge", "company_id": company_id, "canonical": canon,
                        "survivor": str(survivor.id),
                        "absorbed": [{"thread_id": str(t.id), "mobile_no": t.mobile_no}
                                     for t in threads if t.id != survivor.id]})
    return actions, problems


def apply_contacts(db, changes) -> None:
    for c in changes:
        db.execute(text("UPDATE phonebook_contacts SET mobile_no = :n WHERE id = :i"),
                   {"n": c["new"], "i": c["contact_id"]})


def apply_threads(db, actions) -> None:
    for a in actions:
        if a["type"] == "rekey":
            db.execute(text("UPDATE agent_threads SET mobile_no = :n WHERE id = :i"),
                       {"n": a["new"], "i": a["thread_id"]})
            continue

        survivor = a["survivor"]
        absorbed = [x["thread_id"] for x in a["absorbed"]]
        ids = [survivor] + absorbed
        # Combine identity and clocks onto the survivor before the others go.
        db.execute(text("""
            UPDATE agent_threads s SET
                contact_name    = COALESCE(s.contact_name,
                                    (SELECT contact_name FROM agent_threads
                                      WHERE id = ANY(CAST(:ids AS uuid[]))
                                        AND contact_name IS NOT NULL LIMIT 1)),
                last_inbound_at = (SELECT MAX(last_inbound_at) FROM agent_threads
                                    WHERE id = ANY(CAST(:ids AS uuid[]))),
                last_message_at = (SELECT MAX(last_message_at) FROM agent_threads
                                    WHERE id = ANY(CAST(:ids AS uuid[])))
             WHERE s.id = :s
        """), {"ids": ids, "s": survivor})
        # Move every message to the survivor and renumber in time order. Two steps
        # through negative values, so the (thread_id, seq) unique constraint never sees
        # two rows at one position mid-update: every temporary value is distinct and
        # cannot collide with a positive one.
        db.execute(text("""
            WITH ordered AS (
                -- Time order; ties (same transaction, same now()) keep each
                -- thread's own order via its seq.
                SELECT id, ROW_NUMBER() OVER (ORDER BY created_at, seq, id) AS n
                  FROM agent_messages WHERE thread_id = ANY(CAST(:ids AS uuid[])))
            UPDATE agent_messages m SET thread_id = :s, seq = -o.n
              FROM ordered o WHERE m.id = o.id
        """), {"s": survivor, "ids": ids})
        db.execute(text("""UPDATE agent_messages SET seq = -seq, rev = -seq
                            WHERE thread_id = :s"""), {"s": survivor})
        db.execute(text("""
            UPDATE agent_threads SET
                message_seq = (SELECT COUNT(*) FROM agent_messages WHERE thread_id = :s),
                rev         = (SELECT COUNT(*) FROM agent_messages WHERE thread_id = :s),
                mobile_no   = NULL
             WHERE id = :s
        """), {"s": survivor})
        db.execute(text("DELETE FROM agent_threads WHERE id = ANY(CAST(:ids AS uuid[]))"),
                   {"ids": absorbed})
        db.execute(text("UPDATE agent_threads SET mobile_no = :m WHERE id = :s"),
                   {"m": a["canonical"], "s": survivor})


def run(db, apply: bool) -> dict:
    regions = _regions(db)
    contact_changes, contact_problems = plan_contacts(db, regions)
    thread_actions, thread_problems = plan_threads(db, regions)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "applied": apply,
        "contacts": {"changes": contact_changes, "problems": contact_problems},
        "threads": {"actions": thread_actions, "problems": thread_problems},
    }
    if apply:
        apply_contacts(db, contact_changes)
        apply_threads(db, thread_actions)
        db.commit()
    else:
        db.rollback()
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    ap.add_argument("--log", default="normalize_numbers_report.json",
                    help="where to write the JSON report")
    args = ap.parse_args(argv)

    db = SessionLocal()
    try:
        report = run(db, apply=args.apply)
    finally:
        db.close()

    with open(args.log, "w") as fh:
        json.dump(report, fh, indent=2)
    merges = sum(1 for a in report["threads"]["actions"] if a["type"] == "merge")
    rekeys = sum(1 for a in report["threads"]["actions"] if a["type"] == "rekey")
    print(f"{'APPLIED' if args.apply else 'DRY RUN'} — "
          f"contacts to rewrite: {len(report['contacts']['changes'])}, "
          f"contact problems: {len(report['contacts']['problems'])}, "
          f"threads to merge: {merges}, threads to re-key: {rekeys}, "
          f"thread problems: {len(report['threads']['problems'])}. "
          f"Report: {args.log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
