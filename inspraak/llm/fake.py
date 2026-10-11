"""Deterministic stand-in provider. No network, no key.

Per purpose it builds an answer from `context` (the same material the prompt was built
from), so downstream checks behave like the real thing: the analyst's quotes are real
passages of the source text (plus one invented quote that the verifier must drop), the
drafter writes from the retrieved chunks, the critic marks the sentence that cites
nothing as unsupported. A test can override any purpose with `handlers`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from inspraak.llm.base import Provider, RawCompletion

Handler = Callable[[str, type[BaseModel], dict[str, Any]], dict[str, Any]]

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def first_sentences(text: str, n: int) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(" ".join(text.split())) if s.strip()][:n]


def _summary(prompt: str, schema: type[BaseModel], ctx: dict[str, Any]) -> dict[str, Any]:
    chunks = ctx.get("chunks") or []
    title = ctx.get("title") or "the publication"
    points = [first_sentences(c, 1)[0] for c in chunks[:4] if first_sentences(c, 1)]
    return {
        "one_paragraph": f"The text titled {title} sets out rules described in {len(chunks)} "
        "retrieved passages. This is a deterministic stub summary.",
        "key_points": points,
        "affected_groups": ["respondents named in the text"],
        "what_is_asked": "Comments on the proposal within the feedback period.",
    }


def _analysis(prompt: str, schema: type[BaseModel], ctx: dict[str, Any]) -> dict[str, Any]:
    source = ctx.get("source_text") or ""
    sentences = [s for s in first_sentences(source, 6) if len(s) >= 20]
    asks = [{"ask": f"Ask {i + 1}: {s[:60]}", "quote": s} for i, s in enumerate(sentences[:2])]
    asks.append({"ask": "An invented ask", "quote": "This sentence is not in the source text."})
    lowered = source.lower()
    if "oppose" in lowered or "reject" in lowered:
        stance, confidence = "opposes", 0.8
    elif "welcome" in lowered or "support" in lowered:
        stance, confidence = "supports", 0.8
    elif sentences:
        stance, confidence = "wants_changes", 0.7
    else:
        stance, confidence = "unclear", 0.2
    return {"stance": stance, "confidence": confidence, "key_asks": asks}


def _draft(prompt: str, schema: type[BaseModel], ctx: dict[str, Any]) -> dict[str, Any]:
    org = ctx.get("organisation_name") or "Our organisation"
    chunks = ctx.get("chunks") or []
    lines = [f"{org} welcomes the opportunity to comment on this proposal."]
    ids: list[int] = []
    for c in chunks[:3]:
        head = first_sentences(c["text"], 1)
        if head:
            # real drafters copy the prompt's retrieval markers; the pipeline must cut them
            lines.append(
                f"On the passage stating '{head[0][:120]}' we ask for clarity [chunk:{c['id']}]."
            )
            ids.append(int(c["id"]))
    lines.append("We also believe the moon landing budget should be doubled.")  # unsupported
    return {"text": " ".join(lines), "stance": "wants_changes", "chunk_ids_used": ids}


def _critic(prompt: str, schema: type[BaseModel], ctx: dict[str, Any]) -> dict[str, Any]:
    sentences = ctx.get("sentences") or []
    chunk_ids = [int(c["id"]) for c in (ctx.get("chunks") or [])]
    out = []
    for i, s in enumerate(sentences):
        if "moon landing" in s:
            out.append({"index": i, "supported": False, "source": "none", "note": "no source"})
        elif "welcomes the opportunity" in s:
            out.append({"index": i, "supported": True, "source": "profile", "note": ""})
        elif "we ask for" in s.lower():
            # a position: the passage plus the organisation's own goals
            source = f"chunk:{chunk_ids[0]}, profile" if chunk_ids else "profile"
            out.append({"index": i, "supported": True, "source": source, "note": ""})
        else:
            source = f"chunk:{chunk_ids[0]}" if chunk_ids else "profile"
            out.append({"index": i, "supported": True, "source": source, "note": ""})
    return {"sentences": out}


_WORD = re.compile(r"[a-z]{4,}")


def _rerank(prompt: str, schema: type[BaseModel], ctx: dict[str, Any]) -> dict[str, Any]:
    """Relevance from the share of candidate words that also occur in the profile, in
    [0.4, 0.59]: the stub reorders candidates, drops none and never claims a strong fit
    (every card stays in the uncertain band)."""
    profile = set(_WORD.findall((ctx.get("profile_text") or "").lower()))
    items = []
    for c in ctx.get("candidates") or []:
        words = set(_WORD.findall(f"{c.get('short_title', '')} {c.get('title', '')}".lower()))
        shared = sorted(words & profile)
        relevance = round(0.4 + 0.19 * len(shared) / max(len(words), 1), 3)
        reason = f"Shares {len(shared)} words with the profile" + (
            f": {', '.join(shared[:3])}." if shared else "."
        )
        items.append({"publication": c["id"], "relevance": relevance, "reason": reason})
    return {"items": items}


def _fill(schema_json: dict[str, Any], defs: dict[str, Any]) -> Any:
    """A schema-valid value for any schema, for purposes the fake does not know."""
    if "$ref" in schema_json:
        return _fill(defs[schema_json["$ref"].split("/")[-1]], defs)
    if "anyOf" in schema_json:
        return _fill(schema_json["anyOf"][0], defs)
    if "enum" in schema_json:
        return schema_json["enum"][0]
    if "default" in schema_json:
        return schema_json["default"]
    kind = schema_json.get("type")
    if kind == "object":
        return {k: _fill(v, defs) for k, v in (schema_json.get("properties") or {}).items()}
    if kind == "array":
        return []
    if kind == "string":
        return "stub"
    if kind == "integer":
        return 0
    if kind == "number":
        return float(schema_json.get("minimum", 0.0))
    if kind == "boolean":
        return False
    return None


DEFAULT_HANDLERS: dict[str, Handler] = {
    "reader_summary": _summary,
    "feedback_analysis": _analysis,
    "draft": _draft,
    "critic": _critic,
    "match_rerank": _rerank,
}


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, handlers: dict[str, Handler] | None = None, **kwargs) -> None:
        kwargs.setdefault("model", "fake-1")
        super().__init__(**kwargs)
        self.handlers = {**DEFAULT_HANDLERS, **(handlers or {})}
        self.prompts: list[tuple[str, str]] = []  # (purpose, prompt) for assertions
        self._purpose: str = ""

    def complete_json(self, prompt, schema, *, purpose, **kwargs):  # type: ignore[override]
        self._purpose = purpose
        self.prompts.append((purpose, prompt))
        return super().complete_json(prompt, schema, purpose=purpose, **kwargs)

    def _generate(
        self,
        prompt: str,
        schema: type[BaseModel],
        *,
        system: str | None,
        temperature: float,
        max_output_tokens: int,
        context: dict[str, Any] | None,
    ) -> RawCompletion:
        handler = self.handlers.get(self._purpose)
        if handler is not None:
            payload = handler(prompt, schema, context or {})
        else:
            js = schema.model_json_schema()
            payload = _fill(js, js.get("$defs", {}))
        text = json.dumps(payload)
        return RawCompletion(
            text=text, prompt_tokens=len(prompt) // 4, output_tokens=len(text) // 4
        )
