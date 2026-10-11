"""The local demo seed: the fixture snapshot, the publication layer on the deterministic
provider and one run waiting at the go / no-go, from one call."""

from __future__ import annotations

import pytest

from inspraak.seed import SEED_ORGANISATION, SEED_PUBLICATION, seed_demo, snapshot_is_empty

pytestmark = pytest.mark.db


def test_seed_demo_builds_a_working_snapshot(conn, agent_env):
    assert snapshot_is_empty(conn)
    out = seed_demo(conn)
    assert not snapshot_is_empty(conn)
    assert out["snapshot"]["publications_in_scope"] == 1
    assert out["publication_layer"]["calls"] >= 1  # summary and analyses, deterministic
    run = conn.execute(
        "SELECT status, stage FROM runs WHERE id = %s AND tr_id = %s",
        (out["run"], SEED_ORGANISATION),
    ).fetchone()
    assert (run["status"], run["stage"]) == ("waiting", "go_no_go")
    item = conn.execute(
        "SELECT 1 FROM review_items WHERE run_id = %s AND kind = 'go_no_go' AND status = 'open'",
        (out["run"],),
    ).fetchone()
    assert item is not None
    summary = conn.execute(
        "SELECT 1 FROM publication_summaries WHERE publication_id = %s", (SEED_PUBLICATION,)
    ).fetchone()
    assert summary is not None
