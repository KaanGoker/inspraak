"""Finish the publication layer with a chain of CLI providers (default agy, then the
Claude CLI). Idempotent: run it again after a usage limit and it continues.
Usage: python scripts/backfill.py [providers] [--reverse]"""

import json
import sys
import time

from inspraak.db import connect
from inspraak.jobs import precompute_publication, publications_to_precompute
from inspraak.jobs.batch import analyse_parallel, pending_feedback_ids
from inspraak.llm import make_provider


def log(m):
    print(time.strftime("%H:%M:%S"), m, flush=True)


chain = (
    sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "agy,claude_cli"
).split(",")
reverse = "--reverse" in sys.argv
WORKERS = {"agy": 30, "claude_cli": 20}


def stage(fn):
    """Run fn(provider) down the chain until it completes without a quota stop."""
    while chain:
        p = make_provider(chain[0])
        if fn(p):
            return True
        log(f"{chain[0]} spent, next in chain")
        chain.pop(0)
    log("STOPPED: every provider spent")
    sys.exit(2)


def batched(p):
    s = analyse_parallel(p, workers=WORKERS.get(p.name, 10), reverse=reverse, log=log)
    log(f"batched {p.name}: {json.dumps(s)}")
    return not s["stopped"]


def single(p):
    s = analyse_parallel(p, workers=WORKERS.get(p.name, 10), single=True, log=log)
    log(f"single {p.name}: {json.dumps(s)}")
    return not s["stopped"]


def summaries(p):
    with connect() as c:
        for pid in publications_to_precompute(c):
            st = precompute_publication(c, p, pid, feedback_limit=0)
            if st.get("calls"):
                log(f"publication {pid}: {json.dumps(st)}")
            if st.get("stopped"):
                return False
    return True


stage(batched)
stage(single)
stage(summaries)
with connect() as c:
    log(f"DONE, pending submissions left: {len(pending_feedback_ids(c))}")
