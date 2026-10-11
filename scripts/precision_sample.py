"""Draw the hand-check sample of docs/EVAL.md: 30 organisation and consultation pairs,
`random.seed(42)`, from the match lists of the demo runs started by one `inspraak demo`
pass. Prints each pair with the register profile and the consultation text the judge
reads. Usage: python scripts/precision_sample.py [--since 'YYYY-MM-DD HH:MM'] [--n 30]"""

import argparse
import json
import random

from inspraak.db import connect

parser = argparse.ArgumentParser()
parser.add_argument("--since", help="runs created at or after this time (the demo pass)")
parser.add_argument("--n", type=int, default=30)
args = parser.parse_args()

with connect() as conn:
    runs = conn.execute(
        "SELECT r.id, r.tr_id, o.name, r.result FROM runs r JOIN organisations o "
        "ON o.tr_id = r.tr_id WHERE r.requested_by = 'demo' AND r.stage <> 'superseded' "
        + ("AND r.created_at >= %s " if args.since else "")
        + "ORDER BY r.created_at",
        (args.since,) if args.since else (),
    ).fetchall()
    pairs = []
    for r in runs:
        for rank, m in enumerate((r["result"] or {}).get("matches") or [], start=1):
            pairs.append((r["tr_id"], r["name"], rank, m))
    random.seed(42)
    sample = sorted(random.sample(pairs, min(args.n, len(pairs))), key=lambda p: (p[1], p[2]))
    out = []
    for tr_id, name, rank, m in sample:
        org = conn.execute(
            "SELECT category, goals, interests, interest_represented FROM organisations "
            "WHERE tr_id = %s",
            (tr_id,),
        ).fetchone()
        summary = conn.execute(
            "SELECT summary->>'one_paragraph' AS s FROM publication_summaries "
            "WHERE publication_id = %s",
            (m["publication_id"],),
        ).fetchone()
        out.append(
            {
                "organisation": name,
                "tr_id": tr_id,
                "rank": rank,
                "publication_id": m["publication_id"],
                "consultation": m.get("short_title") or m.get("title"),
                "score": m.get("score"),
                "relevance": m.get("relevance"),
                "band": m.get("band"),
                "reason": m.get("reason"),
                "category": org["category"],
                "goals": (org["goals"] or "")[:1200],
                "interests": org["interests"],
                "summary": (summary["s"] if summary else "")[:500],
            }
        )
    print(
        json.dumps(
            {"runs": len(runs), "pairs": len(pairs), "sample": out}, indent=1, ensure_ascii=False
        )
    )
