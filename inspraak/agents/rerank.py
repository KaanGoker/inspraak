"""Matcher rerank: one batched model call per organisation judges the embedding
candidates against the register profile, returning a relevance in [0, 1] and a one-line
reason per consultation.

Judgments are cached per (organisation, publication, profile text) in `match_reranks`
and kept forever, so a page read never calls a model: without a provider the reranker
only reads the cache, and the matcher falls back to the embedding order when a candidate
has no cached judgment.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import psycopg

from inspraak.llm import LLMError, Provider, QuotaExhausted
from inspraak.llm.schemas import RerankBatch

PURPOSE = "match_rerank"
GOALS_CHARS = 1500
SUMMARY_CHARS = 600

SYSTEM = (
    "You decide which EU consultations an organisation would plausibly answer. "
    "Judge only from the organisation's register profile and the consultation text given."
)

RULE = (
    "Rule: a consultation is relevant when its subject sits inside the organisation's "
    "stated mission, so that an organisation of that kind would plausibly answer it. A long "
    "list of ticked interest areas alone does not make a narrow technical act relevant. A "
    "large horizontal file (public procurement, taxation, a general AI or data act) can be "
    "relevant for a general business federation or a consultancy. Scale: 0.9 to 1.0 core "
    "subject of the organisation; 0.6 to 0.8 clearly inside its mission; 0.3 to 0.5 "
    "plausible but peripheral; 0.0 to 0.2 unrelated. Reasons: one short plain sentence "
    "naming the link or the mismatch, no dashes."
)


def profile_text(profile: dict[str, Any]) -> str:
    """The register profile as the reranker sees it (also the cache key)."""

    def joined(value: Any) -> str:
        if isinstance(value, list | tuple):
            return ", ".join(str(v) for v in value if v)
        return str(value or "").strip()

    lines = [
        f"Name: {joined(profile.get('name'))}"
        + (f" ({joined(profile.get('acronym'))})" if profile.get("acronym") else ""),
        f"Register category: {joined(profile.get('category'))}",
        f"Goals: {joined(profile.get('goals'))[:GOALS_CHARS]}",
        f"Declared interests: {joined(profile.get('interests'))}",
        f"Levels of interest: {joined(profile.get('levels_of_interest'))}",
    ]
    if profile.get("interest_represented"):
        lines.append(f"Interest represented: {joined(profile.get('interest_represented'))}")
    if profile.get("note"):
        lines.append(f"Reviewer note: {joined(profile.get('note'))}")
    return "\n".join(lines)


def profile_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Reranker:
    """Cached relevance judgments for one organisation. `provider=None` reads the cache
    only (page reads); with a provider the missing candidates go into one call."""

    def __init__(
        self,
        conn: psycopg.Connection,
        tr_id: str,
        profile: dict[str, Any],
        provider: Provider | None = None,
        *,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.conn = conn
        self.tr_id = tr_id
        self.text = profile_text(profile)
        self.hash = profile_hash(self.text)
        self.provider = provider
        self.log = log
        self.calls = 0
        self.cached = 0
        self.error: str | None = None

    def cached_scores(self, publication_ids: list[int]) -> dict[int, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT publication_id, relevance, reason, provider, model FROM match_reranks "
            "WHERE tr_id = %s AND profile_hash = %s AND publication_id = ANY(%s)",
            (self.tr_id, self.hash, publication_ids),
        ).fetchall()
        return {
            int(r["publication_id"]): {
                "relevance": float(r["relevance"]),
                "reason": r["reason"],
                "model": r["model"],
            }
            for r in rows
        }

    def scores(self, publication_ids: list[int]) -> dict[int, dict[str, Any]]:
        out = self.cached_scores(publication_ids)
        self.cached = len(out)
        missing = [p for p in publication_ids if p not in out]
        if missing and self.provider is not None:
            try:
                out.update(self._ask(missing))
            except (LLMError, QuotaExhausted, ValueError) as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:300]
                if self.log:
                    self.log(f"rerank {self.tr_id}: {self.error}")
        return out

    def _candidates(self, publication_ids: list[int]) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT p.id, p.type, p.title, i.short_title, s.summary->>'one_paragraph' AS summary "
            "FROM publications p JOIN initiatives i ON i.id = p.initiative_id "
            "LEFT JOIN publication_summaries s ON s.publication_id = p.id "
            "WHERE p.id = ANY(%s) ORDER BY p.id",
            (publication_ids,),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "short_title": r["short_title"] or "",
                "type": r["type"] or "",
                "title": (r["title"] or "")[:240],
                "summary": (r["summary"] or "")[:SUMMARY_CHARS],
            }
            for r in rows
        ]

    def _ask(self, publication_ids: list[int]) -> dict[int, dict[str, Any]]:
        candidates = self._candidates(publication_ids)
        blocks = []
        for c in candidates:
            block = f"[{c['id']}] {c['short_title']} ({c['type']})\nTitle: {c['title']}"
            if c["summary"]:
                block += f"\nSummary: {c['summary']}"
            blocks.append(block)
        prompt = (
            f"Organisation (EU Transparency Register):\n{self.text}\n\n"
            f"Consultations ({len(candidates)}):\n\n"
            + "\n\n".join(blocks)
            + f"\n\n{RULE}\n\nReturn one item per consultation, using its id in brackets "
            "as `publication`."
        )
        result = self.provider.complete_json(  # type: ignore[union-attr]
            prompt,
            RerankBatch,
            purpose=PURPOSE,
            system=SYSTEM,
            context={"profile_text": self.text, "candidates": candidates},
            cache_key=f"{self.tr_id}:{self.hash}",
        )
        self.calls += 1
        wanted = {c["id"] for c in candidates}
        out: dict[int, dict[str, Any]] = {}
        for item in result.data.items:
            if item.publication not in wanted or item.publication in out:
                continue  # an id the prompt did not give, or a repeat: ignored
            out[item.publication] = {
                "relevance": round(float(item.relevance), 3),
                "reason": " ".join(item.reason.split())[:300],
                "model": result.model,
            }
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO match_reranks (tr_id, publication_id, profile_hash, relevance, "
                "reason, provider, model) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tr_id, publication_id, profile_hash) DO NOTHING",
                [
                    (
                        self.tr_id,
                        pid,
                        self.hash,
                        v["relevance"],
                        v["reason"],
                        result.provider,
                        result.model,
                    )
                    for pid, v in out.items()
                ],
            )
        self.conn.commit()
        return out
