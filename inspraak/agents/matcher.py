"""Scout/Matcher: embedding candidates, a hub penalty, one card per initiative, and an
optional model rerank of the strongest candidates.

1. Cosine similarity between the register profile vector and the in-scope publication
   vectors.
2. Hub penalty. Long horizontal texts sit close to almost every profile. Each publication
   carries `hub_score`: its mean cosine to its `HUB_NEIGHBOURS` nearest registrants, minus
   the mean of that value over the in-scope publications (a CSLS style correction).
   `HUB_WEIGHT` times it is subtracted. Tuned on `inspraak eval-match`: recall does not
   drop, and the most frequent publication in the 40 demo lists appears less often.
3. "Not our topic" corrections. A corrected publication is excluded. Only its unusually
   close neighbours lose score: the penalty counts only the similarity above a baseline
   (mean plus one standard deviation of the corrected publication's similarity to the
   candidates), scaled to [0, 1] and times `CORRECTION_WEIGHT`.
4. One card per initiative. Publications of the same initiative (or with the same short
   title) collapse into one entry: the open one with the nearest deadline represents it,
   with the best score of the group; the others are listed as `siblings`.
5. Rerank. With a `Reranker`, the `RERANK_POOL` strongest cards are ordered by the
   model's relevance (cached per organisation and publication). Cards the model judges
   unrelated (relevance under `RERANK_DROP`) are dropped. Without a reranker, or when a
   candidate has no judgment, the embedding order stands.

Scores in [min_score, uncertain_score) are the uncertain band and raise an escalation;
after a rerank the band follows the model's relevance instead.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

import numpy as np
import psycopg

from inspraak.config import portal_now
from inspraak.store import to_array

if TYPE_CHECKING:
    from inspraak.agents.rerank import Reranker

CORRECTION_WEIGHT = 0.3
FALLBACK_BASELINE = 0.6  # used when there are too few candidates to estimate one
HUB_WEIGHT = 0.5
HUB_NEIGHBOURS = 200
RERANK_POOL = 15
RERANK_DROP = 0.3  # a reranked card under this relevance is not shown
RERANK_MATCH = 0.6  # a reranked card at or above this relevance is in the match band
UNJUDGED = 0.5  # sort position of a candidate the model skipped


def correction_penalty(matrix: np.ndarray, corrected: np.ndarray) -> np.ndarray:
    """Per-row penalty from the corrected publication vectors (rows of `corrected`)."""
    sims = matrix @ corrected.T  # (n candidates, n corrected)
    if sims.shape[0] >= 5:
        baseline = sims.mean(axis=0) + sims.std(axis=0)
    else:
        baseline = np.full(sims.shape[1], FALLBACK_BASELINE, dtype=np.float32)
    baseline = np.clip(baseline, 0.0, 0.95)
    excess = np.clip(sims - baseline, 0, None) / (1.0 - baseline)
    return CORRECTION_WEIGHT * excess.max(axis=1)


def hub_scores(
    org_matrix: np.ndarray, pub_matrix: np.ndarray, k: int = HUB_NEIGHBOURS
) -> np.ndarray:
    """Per publication: mean cosine to its k nearest organisations, centred on the mean."""
    sims = org_matrix @ pub_matrix.T  # (n orgs, n pubs)
    k = max(1, min(k, sims.shape[0]))
    top = np.partition(sims, sims.shape[0] - k, axis=0)[-k:]
    hub = top.mean(axis=0)
    return (hub - hub.mean()).astype(np.float32)


def compute_hub_scores(conn: psycopg.Connection, *, k: int = HUB_NEIGHBOURS) -> dict[str, Any]:
    """Recompute `publications.hub_score` for the in-scope publications with a vector.
    Needs the organisation vectors; run after `inspraak embed`."""
    pubs = conn.execute(
        "SELECT id, embedding FROM publications WHERE in_scope AND embedding IS NOT NULL "
        "ORDER BY id"
    ).fetchall()
    orgs = conn.execute(
        "SELECT profile_embedding FROM organisations WHERE profile_embedding IS NOT NULL"
    ).fetchall()
    if not pubs or not orgs:
        return {"publications": len(pubs), "organisations": len(orgs), "updated": 0}
    pub_matrix = np.stack([to_array(p["embedding"]) for p in pubs])
    org_matrix = np.stack([to_array(o["profile_embedding"]) for o in orgs])
    hub = hub_scores(org_matrix, pub_matrix, k)
    conn.execute("UPDATE publications SET hub_score = NULL WHERE NOT in_scope")
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE publications SET hub_score = %s WHERE id = %s",
            [(float(h), p["id"]) for h, p in zip(hub, pubs, strict=True)],
        )
    conn.commit()
    order = np.argsort(-hub)
    return {
        "publications": len(pubs),
        "organisations": len(orgs),
        "neighbours": k,
        "updated": len(pubs),
        "strongest_hubs": [(int(pubs[i]["id"]), round(float(hub[i]), 4)) for i in order[:5]],
    }


_SPACES = re.compile(r"\W+")


def _title_key(title: str | None) -> str:
    return _SPACES.sub(" ", (title or "").lower()).strip()


def collapse_initiatives(
    cards: list[dict[str, Any]], prefer: set[int] | frozenset[int] = frozenset()
) -> list[dict[str, Any]]:
    """One card per initiative (or identical short title). The representative is a
    publication in `prefer` (one with a draft) when the group has one, else the open one
    with the nearest deadline; the card keeps the best score of the group. Input order
    does not matter; output is sorted by score."""
    groups: dict[str, list[dict[str, Any]]] = {}
    alias: dict[str, str] = {}
    for c in cards:
        keys = [f"i:{c['initiative_id']}"]
        if c.get("short_title"):
            keys.append(f"t:{_title_key(c['short_title'])}")
        root = next((alias[k] for k in keys if k in alias), keys[0])
        for k in keys:
            alias.setdefault(k, root)
        groups.setdefault(root, []).append(c)
    out = []
    for members in groups.values():
        best = max(members, key=lambda m: m["score"])

        def representative_key(m: dict[str, Any]) -> tuple:
            is_open = m.get("feedback_status") == "OPEN"
            end = m.get("end_date") or "9999"
            return (m["publication_id"] not in prefer, not is_open, end, -m["score"])

        rep = dict(min(members, key=representative_key))
        rep["score"] = best["score"]
        rep["raw_score"] = max(m["raw_score"] for m in members)
        rep["band"] = best["band"]
        rep["siblings"] = sorted(
            m["publication_id"] for m in members if m["publication_id"] != rep["publication_id"]
        )
        out.append(rep)
    out.sort(key=lambda m: -m["score"])
    return out


def match(
    conn: psycopg.Connection,
    profile_vector: np.ndarray,
    *,
    top_k: int,
    min_score: float,
    uncertain_score: float,
    corrections: list[int] | None = None,
    open_only: bool = True,
    now: datetime | None = None,
    reranker: Reranker | None = None,
    pool: int = RERANK_POOL,
    hub_weight: float = HUB_WEIGHT,
    keep: list[int] | None = None,
) -> list[dict[str, Any]]:
    """The cards for one profile, best first. Publications in `keep` (those with a draft)
    stay on the list even when the rerank or `top_k` would cut them, marked `kept`."""
    keep = list(keep or [])
    now = now or portal_now()
    sql = (
        "SELECT p.id, p.initiative_id, i.short_title, p.type, p.title, p.end_date, "
        "p.feedback_status, p.org_feedback_count, p.citizen_feedback_count, p.hub_score, "
        "p.embedding FROM publications p JOIN initiatives i ON i.id = p.initiative_id "
        "WHERE p.in_scope AND p.embedding IS NOT NULL"
    )
    params: list[Any] = []
    if open_only:
        sql += " AND p.feedback_status = 'OPEN' AND p.end_date >= %s"
        params.append(now)
    rows = conn.execute(sql + " ORDER BY p.id", params).fetchall()
    if not rows:
        return []
    matrix = np.stack([to_array(r["embedding"]) for r in rows])
    raw_scores = matrix @ profile_vector.astype(np.float32)
    hub = np.array([r["hub_score"] or 0.0 for r in rows], dtype=np.float32)
    scores = raw_scores - hub_weight * hub
    corrections = corrections or []
    if corrections:
        corrected = conn.execute(
            "SELECT embedding FROM publications WHERE id = ANY(%s) AND embedding IS NOT NULL",
            (corrections,),
        ).fetchall()
        if corrected:
            cm = np.stack([to_array(c["embedding"]) for c in corrected])
            scores = scores - correction_penalty(matrix, cm)
    cards: list[dict[str, Any]] = []
    for row, raw, score in zip(rows, raw_scores, scores, strict=True):
        if row["id"] in corrections or score < min_score:
            continue
        end = row["end_date"]
        deadline_days = (end - now).days if end else None
        cards.append(
            {
                "publication_id": row["id"],
                "initiative_id": row["initiative_id"],
                "short_title": row["short_title"],
                "title": row["title"],
                "type": row["type"],
                "feedback_status": row["feedback_status"],
                "end_date": end.isoformat() if end else None,
                "deadline_days": deadline_days,
                "score": round(float(score), 4),
                "raw_score": round(float(raw), 4),
                "band": "match" if score >= uncertain_score else "uncertain",
                "org_feedback_count": row["org_feedback_count"],
                "citizen_feedback_count": row["citizen_feedback_count"],
            }
        )
    cards = collapse_initiatives(cards, prefer=set(keep))
    for c in cards:  # the deadline of the representative, not of the best-scoring sibling
        c["deadline_days"] = (
            (datetime.fromisoformat(c["end_date"]) - now).days if c["end_date"] else None
        )
    candidates = cards[: max(pool, top_k)]
    if reranker is not None and candidates:
        judged = reranker.scores([c["publication_id"] for c in candidates])
        if judged:
            for c in candidates:
                j = judged.get(c["publication_id"])
                if j is None:  # the model skipped it: neutral, keeps its embedding band
                    continue
                c["relevance"] = j["relevance"]
                c["reason"] = j["reason"]
                c["rerank_model"] = j.get("model")
                c["band"] = "match" if j["relevance"] >= RERANK_MATCH else "uncertain"
            candidates = [c for c in candidates if c.get("relevance", 1.0) >= RERANK_DROP]
            candidates.sort(key=lambda c: (-c.get("relevance", UNJUDGED), -c["score"]))
    out = candidates[:top_k]
    for pid in keep:
        if not any(c["publication_id"] == pid for c in out):
            card = next((c for c in cards if c["publication_id"] == pid), None)
            if card is not None:
                out.append({**card, "kept": "draft"})
    for rank, c in enumerate(out, start=1):
        c["rank"] = rank
        c["reranked"] = "relevance" in c
    return out
