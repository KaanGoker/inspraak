"""Nodes of the organisation-layer graph. Each node opens its own short connection,
writes one trace row, and returns the part of the state it produced. Only the Drafter and
the Critic call the model (one call each per consultation the reviewer said go to); the
Reader falls back to one shared, cached call when a publication has no summary yet.
"""

from __future__ import annotations

import re
import time
from typing import Any

import psycopg
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt
from psycopg.types.json import Jsonb

from inspraak import db, store
from inspraak.agents import memory
from inspraak.agents.matcher import match
from inspraak.agents.rerank import Reranker
from inspraak.agents.state import RunState, jsonable
from inspraak.agents.trace import write_trace
from inspraak.config import Settings
from inspraak.jobs.analyst import analyses_for_publication
from inspraak.jobs.meetings_map import lobby_picture
from inspraak.jobs.reader import summarise_publication
from inspraak.llm.base import Provider
from inspraak.llm.schemas import CriticReport, DraftResponse
from inspraak.textutil import plain_dashes, strip_chunk_markers

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
READER_CHUNKS = 8
VOICE_EXCERPT_CHARS = 1200
VOICE_SUBMISSIONS = 2

DRAFTER_SYSTEM = (
    "You draft responses to European Commission 'Have Your Say' consultations on behalf of a "
    "Brussels organisation, in its own voice. Use only the material given: the organisation's "
    "register profile and past submissions, the proposal passages, the summary. Never invent "
    "figures, articles or events. Do not claim the organisation supports, welcomes or opposes "
    "the proposal, or that its benefits outweigh its costs, unless its past submissions or "
    "profile say so; otherwise state its interests and asks neutrally. Stay within 3,800 "
    "characters. Plain hyphens only, never long dashes. The result is a draft for a human to "
    "review; it is never submitted automatically."
)
CRITIC_SYSTEM = (
    "You check a draft sentence by sentence against the material it was written from. A "
    "sentence is supported when a listed passage states it (cite 'chunk:<id>') or when it only "
    "restates the organisation's own profile, past positions or a courtesy formula (cite "
    "'profile'). Anything else is unsupported ('none'). Be strict about facts and figures. An "
    "evaluation (supports, welcomes, opposes, costs outweighed by benefits, the proposal is "
    "right) needs a past submission of the organisation that states it; the profile alone is "
    "not enough. A normative sentence in the organisation's voice (we support, we call for, the "
    "Commission must, rules should) needs the organisation's own source: a past submission "
    "(cite 'submission:<id>') or its register goals (cite 'profile'). A Commission passage "
    "alone does not make it the organisation's position. A sentence may cite several sources, "
    "separated by commas, for example 'chunk:12, profile'."
)
# Evaluative wording: a sentence like this backed only by 'profile' is removed by the critic,
# because the register profile states goals, never a position on a given proposal.
EVALUATIVE = re.compile(
    r"\b(strongly|firmly|fully|warmly|wholeheartedly|welcome[sd]?|support(s|ed)?|"
    r"endorse[sd]?|oppose[sd]?|reject(s|ed)?|outweigh(s|ed)?|correctly|rightly|commend[s]?|"
    r"applaud[s]?|praise[sd]?)\b",
    re.IGNORECASE,
)


COURTESY = re.compile(
    r"\bwelcome[sd]?\s+the\s+(opportunity|chance|possibility|invitation)\b", re.IGNORECASE
)


def is_evaluative(sentence: str) -> bool:
    """Stance wording, not counting courtesy formulas ("welcomes the opportunity to ...")."""
    return bool(EVALUATIVE.search(COURTESY.sub("", sentence)))


# Normative wording: what someone ought to do, or a stance verb with the organisation as
# subject. In a draft it states the organisation's position, so a Commission passage alone
# cannot back it.
NORMATIVE = re.compile(
    r"\b(must|should|shall|ought to|needs? to|calls? (on|for|upon)|urges?|demands?|"
    r"insists?|it is (essential|crucial|vital|necessary|important) (that|to))\b",
    re.IGNORECASE,
)
STANCE_VERBS = (
    r"(support|welcome|endorse|oppose|reject|call|urge|ask|request|recommend|propose|"
    r"believe|regret|insist|want)\w*"
)
# ...unless the sentence reports what the Commission's text says ("Under Article 5,
# providers must ...", "The proposal requires that ...").
REPORTED = re.compile(
    r"\b(under|according to|in line with) (the )?(proposal|proposed|draft|regulation|directive|"
    r"article|annex|act|text|impact assessment)"
    r"|\b(the )?(draft |proposed )?(proposal|regulation|directive|act|text|impact assessment|"
    r"initiative|commission|article \d+\w*)\s+(\w+\s+){0,4}?(states?|says|requires?|"
    r"provides?|proposes?|sets out|stipulates?|specif(y|ies)|introduces?|obliges?|mandates?|"
    r"would|will|aims?)\b",
    re.IGNORECASE,
)


def is_normative(sentence: str, subjects: tuple[str, ...] = ()) -> bool:
    """A position in the organisation's own voice (we support, we call for, must, should),
    not a report of what the Commission's text says. `subjects` are the organisation's name
    and acronym ("BEUC welcomes ..."). Courtesy formulas do not count."""
    text = COURTESY.sub("", sentence)
    if REPORTED.search(text):
        return False
    if NORMATIVE.search(text):
        return True
    names = "|".join(re.escape(n) for n in subjects if n and len(n) > 1)
    who = r"we|our members|our organisation" + (f"|{names}" if names else "")
    return bool(re.search(rf"\b({who})\b[^.;]{{0,40}}?\b{STANCE_VERBS}", text, re.IGNORECASE))


def parse_sources(value: str | None) -> list[str]:
    """The critic's source field: one or several of chunk:<id>, submission:<id>, profile,
    none, separated by commas, semicolons or spaces."""
    tokens = re.findall(r"(?:chunk|submission)\s*:\s*\d+|profile|none", (value or "").lower())
    return [re.sub(r"\s+", "", t) for t in tokens]


def judge_sentence(
    sentence: str,
    cited: list[str],
    known: set[str],
    model_supported: bool,
    note: str = "",
    subjects: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Apply the code's rules to one critic verdict. Returns supported, the valid sources
    and the note shown when the sentence is removed."""
    valid = [c for c in cited if c in known]
    if not model_supported:
        return {"supported": False, "sources": [], "note": note or "no source"}
    if not valid:
        return {
            "supported": False,
            "sources": [],
            "note": "cited a source that is not among the retrieved passages",
        }
    own = [c for c in valid if c == "profile" or c.startswith("submission:")]
    if is_evaluative(sentence) and set(valid) == {"profile"}:
        return {
            "supported": False,
            "sources": [],
            "note": "evaluation backed only by the profile; the stance is the reviewer's",
        }
    if is_normative(sentence, subjects) and not own:
        return {
            "supported": False,
            "sources": [],
            "note": "position in the organisation's voice backed only by a Commission passage",
        }
    return {"supported": True, "sources": valid, "note": note}


# A full stop after one of these does not end a sentence (EU drafts cite "Art. 13",
# "No. 5", "e.g." constantly). Compared lower-case, without the final dot.
ABBREVIATIONS = frozenset(
    "art arts no nos nr para paras par p pp e.g i.e cf vs viz approx incl excl "
    "ca fig figs ch vol eds al mr mrs ms dr prof jr sr ltd inc "
    "corp dept govt op ibid subpara pt".split()
)


def _ends_on_abbreviation(piece: str) -> bool:
    if not piece.endswith("."):
        return False
    last = piece.rsplit(None, 1)[-1].lstrip("([\"'").rstrip(".").lower()
    if last in ABBREVIATIONS:
        return True
    # single letters and initials such as "J." or "U.S."
    return bool(re.fullmatch(r"(?:[a-z]\.)*[a-z]", last))


def _landscape_words(v: dict[str, Any]) -> str:
    stances = ", ".join(
        f"{n} {k.replace('_', ' ')}" for k, n in sorted((v.get("stances") or {}).items())
    )
    return f"{v['analysed']} analysed" + (f" ({stances})" if stances else "")


def split_sentences(text: str) -> list[str]:
    """Sentences for the critic. Splits after . ! ? followed by whitespace, then glues back
    pieces that ended on an abbreviation or before a lower-case word or a digit."""
    pieces = [s.strip() for s in _SENTENCE_END.split(" ".join(text.split())) if s.strip()]
    out: list[str] = []
    for piece in pieces:
        if out and (
            _ends_on_abbreviation(out[-1])
            or (out[-1].endswith(".") and (piece[0].islower() or piece[0].isdigit()))
        ):
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return out


def cut_to_limit(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    return (head[: cut + 1] if cut > limit // 2 else head).rstrip()


def route(state: RunState) -> str:
    """Deterministic supervisor routing on what the state holds."""
    if "profile" not in state:
        return "profiler"
    if "matches" not in state:
        return "matcher"
    if not state["matches"]:
        return "memory"
    if "landscape" not in state:
        return "landscape"
    if "lobby" not in state:
        return "lobby"
    if "reading" not in state:
        return "reader"
    if "go_decision" not in state:
        return "go_no_go"
    if not state["go_decision"].get("go"):
        return "memory"
    if "drafts" not in state:
        return "drafter"
    if "critiques" not in state:
        return "critic"
    if "review_decision" not in state:
        return "review"
    return "memory"


class Nodes:
    def __init__(self, provider: Provider, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings

    # ----------------------------------------------------------------- helpers
    def _run(self, state: RunState, agent: str, work, *, tool: str | None = None):
        """Run `work(conn)` with timing, token and call deltas, and a trace row.
        `work` returns (update, input_summary, output_summary, cache_hit)."""
        self.provider.run_id = state.get("run_id")
        started = time.monotonic()
        calls0, pt0, ot0 = (
            self.provider.calls,
            self.provider.prompt_tokens,
            self.provider.output_tokens,
        )
        with db.session(self.settings.database_url) as conn:
            try:
                update, inp, out, cache_hit = work(conn)
            except GraphInterrupt:
                write_trace(
                    conn,
                    state["run_id"],
                    agent,
                    tool="interrupt",
                    input_summary="waiting for the reviewer",
                    output_summary="run paused in the checkpointer",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            except Exception as exc:
                conn.rollback()
                write_trace(
                    conn,
                    state["run_id"],
                    agent,
                    tool=tool,
                    input_summary="error",
                    output_summary=f"{type(exc).__name__}: {exc}",
                    llm_calls=self.provider.calls - calls0,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            write_trace(
                conn,
                state["run_id"],
                agent,
                tool=tool,
                input_summary=inp,
                output_summary=out,
                prompt_tokens=self.provider.prompt_tokens - pt0,
                output_tokens=self.provider.output_tokens - ot0,
                llm_calls=self.provider.calls - calls0,
                cache_hit=cache_hit,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return update

    def _set_stage(self, conn: psycopg.Connection, run_id: str, stage: str) -> None:
        conn.execute(
            "UPDATE runs SET stage = %s, updated_at = now() WHERE id = %s", (stage, run_id)
        )
        conn.commit()

    def _escalate(
        self,
        conn: psycopg.Connection,
        state: RunState,
        kind: str,
        trigger: str,
        publication_id: int | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """A non-blocking review item (deduplicated per run, kind and publication)."""
        exists = conn.execute(
            "SELECT 1 FROM review_items WHERE run_id = %s AND kind = %s "
            "AND publication_id IS NOT DISTINCT FROM %s",
            (state["run_id"], kind, publication_id),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO review_items (run_id, tr_id, publication_id, kind, blocking, "
                "triggers, "
                "payload) VALUES (%s, %s, %s, %s, false, %s, %s)",
                (state["run_id"], state["tr_id"], publication_id, kind, [trigger], Jsonb(payload)),
            )
            conn.commit()
        return {"trigger": trigger, "publication_id": publication_id, **payload}

    # ----------------------------------------------------------------- supervisor
    def supervisor(self, state: RunState) -> dict[str, Any]:
        nxt = route(state)
        with db.session(self.settings.database_url) as conn:
            write_trace(
                conn,
                state["run_id"],
                "supervisor",
                tool="route",
                input_summary=",".join(k for k in state if k not in ("run_id", "tr_id", "options")),
                output_summary=nxt,
            )
            self._set_stage(conn, state["run_id"], nxt)
        return {"next": nxt}

    # ----------------------------------------------------------------- profiler
    def profiler(self, state: RunState) -> dict[str, Any]:
        def work(conn):
            org = store.get_organisation(conn, state["tr_id"])
            if org is None:
                raise ValueError(f"organisation {state['tr_id']} not in the snapshot")
            edits = memory.profile_edits(conn, state["tr_id"])
            profile = jsonable(org)
            profile.pop("has_embedding", None)
            profile["register_values"] = {k: profile.get(k) for k in edits}
            profile.update(edits)
            profile["edited_fields"] = sorted(edits)
            submissions = store.organisation_submissions(conn, state["tr_id"])
            profile["past_submissions"] = [
                {
                    "feedback_id": s["id"],
                    "publication_id": s["publication_id"],
                    "title": s["short_title"] or s["publication_title"],
                    "date": s["date_feedback"].isoformat() if s["date_feedback"] else None,
                    "excerpt": (s["feedback_text_en"] or s["feedback_text"] or "")[
                        :VOICE_EXCERPT_CHARS
                    ],
                }
                for s in submissions
            ]
            return (
                {"profile": profile},
                state["tr_id"],
                f"{org['name']} ({org['category']}), {len(submissions)} past submissions, "
                f"{len(edits)} reviewer edits",
                None,
            )

        return self._run(state, "profiler", work, tool="register+memory")

    # ----------------------------------------------------------------- matcher
    def matcher(self, state: RunState) -> dict[str, Any]:
        opts = state.get("options") or {}

        def work(conn):
            vector = store.organisation_vector(conn, state["tr_id"])
            if vector is None:
                raise ValueError("no profile vector; run `inspraak embed --target organisations`")
            corrections = memory.not_our_topic_ids(conn, state["tr_id"])
            reranker = None
            if bool(opts.get("rerank", self.settings.match_rerank)):
                reranker = Reranker(conn, state["tr_id"], state["profile"], self.provider)
            matches = match(
                conn,
                vector,
                top_k=int(opts.get("top_k", self.settings.match_top_k)),
                min_score=float(opts.get("min_score", self.settings.match_min_score)),
                uncertain_score=float(
                    opts.get("uncertain_score", self.settings.match_uncertain_score)
                ),
                corrections=corrections,
                open_only=bool(opts.get("open_only", True)),
                reranker=reranker,
            )
            escalations = []
            for m in matches:
                if m["band"] == "uncertain":
                    escalations.append(
                        self._escalate(
                            conn,
                            state,
                            "relevance",
                            "relevance in the uncertain band",
                            m["publication_id"],
                            {"score": m["score"]},
                        )
                    )
                if (
                    m["deadline_days"] is not None
                    and m["deadline_days"] < self.settings.deadline_days
                ):
                    escalations.append(
                        self._escalate(
                            conn,
                            state,
                            "deadline",
                            "deadline under 7 days",
                            m["publication_id"],
                            {"deadline_days": m["deadline_days"]},
                        )
                    )
            summary = (
                ", ".join(f"{m['publication_id']}:{m['score']:.2f}" for m in matches) or "none"
            )
            if reranker is None:
                rerank_note = "rerank off"
            else:
                rerank_note = f"rerank: {reranker.cached} cached, {reranker.calls} calls" + (
                    f", failed ({reranker.error})" if reranker.error else ""
                )
            return (
                {"matches": matches, "escalations": escalations},
                f"{len(corrections)} corrections applied; {rerank_note}",
                f"{len(matches)} matches: {summary}",
                (reranker.calls == 0) if reranker is not None else None,
            )

        return self._run(state, "matcher", work, tool="pgvector")

    # ----------------------------------------------------------------- landscape
    def landscape(self, state: RunState) -> dict[str, Any]:
        def work(conn):
            out: dict[str, Any] = {}
            escalations = []
            for m in state["matches"]:
                pid = m["publication_id"]
                counts = store.feedback_counts(conn, pid)
                analyses = analyses_for_publication(conn, pid)
                shown = [a for a in analyses if not a["needs_review"]]
                low = [a for a in analyses if a["needs_review"] and a["source"] != "none"]
                no_text = [a for a in analyses if a["source"] == "none"]
                unparsed = [a for a in no_text if a["attachment_count"]]
                stances: dict[str, int] = {}
                for a in shown:
                    stances[a["stance"]] = stances.get(a["stance"], 0) + 1
                pub = store.get_publication(conn, pid)
                out[str(pid)] = jsonable(
                    {
                        "publication_id": pid,
                        "by_type": counts["by_type"],
                        "by_country": counts["by_country"][:15],
                        "org_feedback_count": pub["org_feedback_count"],
                        "feedback_truncated": pub["feedback_truncated"],
                        "total_feedback": pub["total_feedback"],
                        "analysed": len(analyses),
                        "stances": stances,
                        "submissions": [
                            {
                                "feedback_id": a["feedback_id"],
                                "organization": a["organization"],
                                "user_type": a["user_type"],
                                "country": a["country"],
                                "register_category": a["register_category"],
                                "stance": a["stance"],
                                "confidence": a["confidence"],
                                "key_asks": a["key_asks"],
                            }
                            for a in shown[:40]
                        ],
                        "held_back_low_confidence": len(low),
                        "without_text": len(no_text),
                        "unparsed_attachments": len(unparsed),
                    }
                )
                if low:
                    escalations.append(
                        self._escalate(
                            conn, state, "stance", "low-confidence stance", pid, {"count": len(low)}
                        )
                    )
                if unparsed:
                    escalations.append(
                        self._escalate(
                            conn,
                            state,
                            "attachment",
                            "unparseable attachment",
                            pid,
                            {"count": len(unparsed)},
                        )
                    )
            return (
                {"landscape": out, "escalations": escalations},
                f"{len(state['matches'])} publications",
                "; ".join(f"{k}: {_landscape_words(v)}" for k, v in out.items()),
                True,
            )

        return self._run(state, "landscape", work, tool="feedback_analyses cache")

    # ----------------------------------------------------------------- lobby mapper
    def lobby(self, state: RunState) -> dict[str, Any]:
        def work(conn):
            out: dict[str, Any] = {}
            for m in state["matches"]:
                pid = m["publication_id"]
                picture = lobby_picture(conn, pid, state["tr_id"])
                out[str(pid)] = jsonable(
                    {
                        "publication_id": pid,
                        "by_category": picture["by_category"],
                        "meetings": picture["meetings"][:30],
                        "own_meetings": picture["own_meetings"][:20],
                        "note": picture["note"],
                    }
                )
            return (
                {"lobby": out},
                f"{len(state['matches'])} publications",
                "; ".join(f"{k}: {len(v['meetings'])} meetings" for k, v in out.items()),
                True,
            )

        return self._run(state, "lobby", work, tool="SQL+publication_meetings")

    # ----------------------------------------------------------------- reader
    def reader(self, state: RunState) -> dict[str, Any]:
        def work(conn):
            vector = store.organisation_vector(conn, state["tr_id"])
            out: dict[str, Any] = {}
            hits = 0
            for m in state["matches"]:
                pid = m["publication_id"]
                summary, hit = summarise_publication(conn, self.provider, pid)
                hits += int(hit)
                chunks = (
                    store.similar_chunks(conn, pid, vector, limit=READER_CHUNKS)
                    if vector is not None
                    else []
                )
                out[str(pid)] = jsonable(
                    {
                        "publication_id": pid,
                        "summary": summary["summary"] if summary else None,
                        "summary_cached": hit,
                        "chunks": [
                            {
                                "id": c["id"],
                                "document_id": c["document_id"],
                                "page_no": c["page_no"],
                                "text": c["text"],
                                "score": round(float(c["score"]), 3),
                            }
                            for c in chunks
                        ],
                    }
                )
            n = len(state["matches"])
            return (
                {"reading": out},
                f"{n} publications, profile vector",
                f"{hits}/{n} summaries from cache; "
                + "; ".join(f"{k}: {len(v['chunks'])} chunks" for k, v in out.items()),
                hits == n,
            )

        return self._run(state, "reader", work, tool="publication_summaries+chunks")

    # ----------------------------------------------------------------- interrupt 1
    def go_no_go(self, state: RunState) -> dict[str, Any]:
        def work(conn):
            payload = {
                "kind": "go_no_go",
                "run_id": state["run_id"],
                "tr_id": state["tr_id"],
                "organisation": state["profile"].get("name"),
                "consultations": [
                    {
                        **m,
                        "summary": (state["reading"].get(str(m["publication_id"])) or {}).get(
                            "summary"
                        ),
                        "stances": (state["landscape"].get(str(m["publication_id"])) or {}).get(
                            "stances"
                        ),
                        "escalations": [
                            e["trigger"]
                            for e in state.get("escalations", [])
                            if e.get("publication_id") == m["publication_id"]
                        ],
                    }
                    for m in state["matches"]
                ],
                "decision_format": {
                    "go": "[publication_id, ...]",
                    "no_go": "[publication_id, ...]",
                    "not_our_topic": "[publication_id, ...] (also reweights future matching)",
                    "decided_by": "name",
                },
            }
            self._ensure_blocking_item(conn, state, "go_no_go", payload)
            decision = interrupt(payload)
            decision = _normalise_go(decision, [m["publication_id"] for m in state["matches"]])
            return (
                {"go_decision": decision},
                f"{len(state['matches'])} consultations",
                f"go {decision['go']}, no_go {decision['no_go']}, "
                f"not_our_topic {decision['not_our_topic']}",
                None,
            )

        return self._run(state, "go_no_go", work, tool="interrupt")

    def _ensure_blocking_item(
        self, conn, state: RunState, kind: str, payload: dict[str, Any]
    ) -> None:
        triggers = sorted({e["trigger"] for e in state.get("escalations", [])})
        if kind == "draft_review":
            triggers = ["every draft", *triggers]
        exists = conn.execute(
            "SELECT id FROM review_items WHERE run_id = %s AND kind = %s AND blocking",
            (state["run_id"], kind),
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE review_items SET payload = %s, triggers = %s WHERE id = %s",
                (Jsonb(jsonable(payload)), triggers, exists["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO review_items (run_id, tr_id, kind, blocking, triggers, payload) "
                "VALUES (%s, %s, %s, true, %s, %s)",
                (state["run_id"], state["tr_id"], kind, triggers, Jsonb(jsonable(payload))),
            )
        conn.commit()

    # ----------------------------------------------------------------- drafter
    def drafter(self, state: RunState) -> dict[str, Any]:
        def work(conn):
            profile = state["profile"]
            preferences = memory.draft_preferences(conn, state["tr_id"])
            positions = memory.stored_positions(conn, state["tr_id"])
            drafts: dict[str, Any] = {}
            escalations = []
            for pid in state["go_decision"]["go"]:
                key = str(pid)
                reading = state["reading"].get(key) or {}
                landscape = state["landscape"].get(key) or {}
                m = next(x for x in state["matches"] if x["publication_id"] == pid)
                prompt = _draft_prompt(
                    profile, preferences, m, reading, landscape, self.settings.draft_max_chars
                )
                result = self.provider.complete_json(
                    prompt,
                    DraftResponse,
                    purpose="draft",
                    system=DRAFTER_SYSTEM,
                    context={
                        "organisation_name": profile.get("name"),
                        "chunks": reading.get("chunks", []),
                    },
                    cache_key=f"draft:{state['run_id']}:{pid}",
                    max_output_tokens=self.settings.llm_max_output_tokens,
                )
                text = cut_to_limit(
                    strip_chunk_markers(plain_dashes(result.data.text)),
                    self.settings.draft_max_chars,
                )
                drafts[key] = {
                    "publication_id": pid,
                    "text": text,
                    "stance": result.data.stance,
                    "chunk_ids_used": result.data.chunk_ids_used,
                    "chars": len(text),
                }
                earlier = positions.get(m["initiative_id"])
                if earlier and earlier != result.data.stance:
                    escalations.append(
                        self._escalate(
                            conn,
                            state,
                            "conflict",
                            "conflict with a stored earlier position",
                            pid,
                            {"earlier": earlier, "draft": result.data.stance},
                        )
                    )
            return (
                {"drafts": drafts, "escalations": escalations},
                f"{len(drafts)} consultations, {len(preferences)} stored preferences",
                "; ".join(f"{k}: {v['chars']} chars, {v['stance']}" for k, v in drafts.items()),
                False,
            )

        return self._run(state, "drafter", work, tool="llm")

    # ----------------------------------------------------------------- critic
    def critic(self, state: RunState) -> dict[str, Any]:
        def work(conn):
            critiques: dict[str, Any] = {}
            drafts = dict(state["drafts"])
            escalations = []
            for key, draft in state["drafts"].items():
                pid = draft["publication_id"]
                reading = state["reading"].get(key) or {}
                chunks = reading.get("chunks", [])
                sentences = split_sentences(strip_chunk_markers(draft["text"]))
                voice = _voice(state["profile"])
                result = self.provider.complete_json(
                    _critic_prompt(state["profile"], sentences, chunks, voice),
                    CriticReport,
                    purpose="critic",
                    system=CRITIC_SYSTEM,
                    context={"sentences": sentences, "chunks": chunks, "submissions": voice},
                    cache_key=f"critic:{state['run_id']}:{pid}",
                    max_output_tokens=self.settings.llm_max_output_tokens,
                )
                known = (
                    {f"chunk:{c['id']}" for c in chunks}
                    | {f"submission:{v['feedback_id']}" for v in voice}
                    | {"profile"}
                )
                verdict: dict[int, dict[str, Any]] = {}
                for s in result.data.sentences:
                    if not 0 <= s.index < len(sentences):
                        continue
                    v = judge_sentence(
                        sentences[s.index],
                        parse_sources(s.source),
                        known,
                        bool(s.supported),
                        s.note,
                        subjects=(state["profile"].get("name"), state["profile"].get("acronym")),
                    )
                    verdict[s.index] = {
                        "supported": v["supported"],
                        "source": ", ".join(v["sources"]) if v["supported"] else "none",
                        "sources": v["sources"],
                        "note": v["note"],
                    }
                kept, removed = [], []
                for i, sentence in enumerate(sentences):
                    v = verdict.get(
                        i, {"supported": False, "source": "none", "note": "not checked"}
                    )
                    if v["supported"]:
                        kept.append(sentence)
                    else:
                        removed.append({"index": i, "sentence": sentence, "note": v["note"]})
                checked_text = strip_chunk_markers(
                    cut_to_limit(" ".join(kept), self.settings.draft_max_chars)
                )
                critiques[key] = {
                    "publication_id": pid,
                    "sentences": len(sentences),
                    "supported": len(kept),
                    "removed": removed,
                    "sources": sorted(
                        {src for v in verdict.values() if v["supported"] for src in v["sources"]}
                    ),
                }
                drafts[key] = {
                    **draft,
                    "text": checked_text,
                    "chars": len(checked_text),
                    "machine_text": draft["text"],
                }
                conn.execute(
                    "INSERT INTO drafts (run_id, tr_id, publication_id, version, text, stance, "
                    "critic, status) "
                    "VALUES (%s, %s, %s, 1, %s, %s, %s, 'machine_draft') "
                    "ON CONFLICT (run_id, publication_id, version) DO UPDATE SET text = "
                    "EXCLUDED.text, "
                    "stance = EXCLUDED.stance, critic = EXCLUDED.critic",
                    (
                        state["run_id"],
                        state["tr_id"],
                        pid,
                        checked_text,
                        draft["stance"],
                        Jsonb(critiques[key]),
                    ),
                )
                conn.commit()
                if removed:
                    escalations.append(
                        self._escalate(
                            conn,
                            state,
                            "critic",
                            "critic found unsupported claims",
                            pid,
                            {"removed": len(removed)},
                        )
                    )
            return (
                {"critiques": critiques, "drafts": drafts, "escalations": escalations},
                f"{len(critiques)} drafts",
                "; ".join(
                    f"{k}: {v['supported']}/{v['sentences']} sentences kept"
                    for k, v in critiques.items()
                ),
                False,
            )

        return self._run(state, "critic", work, tool="llm")

    # ----------------------------------------------------------------- interrupt 2
    def review(self, state: RunState) -> dict[str, Any]:
        def work(conn):
            payload = {
                "kind": "draft_review",
                "run_id": state["run_id"],
                "tr_id": state["tr_id"],
                "organisation": state["profile"].get("name"),
                "label": "machine draft, awaiting human review",
                "drafts": [
                    {
                        **d,
                        "critic": state["critiques"].get(k),
                        "title": next(
                            (
                                m["short_title"]
                                for m in state["matches"]
                                if m["publication_id"] == d["publication_id"]
                            ),
                            None,
                        ),
                    }
                    for k, d in state["drafts"].items()
                ],
                "escalations": state.get("escalations", []),
                "decision_format": {
                    "items": {
                        "<publication_id>": {
                            "action": "approve | edit | reject",
                            "text": "edited text when action is edit",
                            "note": "optional",
                        }
                    },
                    "decided_by": "name",
                },
            }
            self._ensure_blocking_item(conn, state, "draft_review", payload)
            decision = interrupt(payload)
            decision = _normalise_review(decision, list(state["drafts"]))
            return (
                {"review_decision": decision},
                f"{len(state['drafts'])} drafts",
                ", ".join(f"{k}: {v['action']}" for k, v in decision["items"].items()),
                None,
            )

        return self._run(state, "review", work, tool="interrupt")

    # ----------------------------------------------------------------- memory
    def memory(self, state: RunState) -> dict[str, Any]:
        def work(conn):
            tr_id, run_id = state["tr_id"], state["run_id"]
            written = 0
            go = state.get("go_decision") or {}
            for kind in ("go", "no_go", "not_our_topic"):
                for pid in go.get(kind, []):
                    memory.remember(
                        conn,
                        tr_id,
                        kind,
                        {"decided_by": go.get("decided_by")},
                        publication_id=pid,
                        run_id=run_id,
                    )
                    written += 1
            review = state.get("review_decision") or {}
            for key, item in review.get("items", {}).items():
                pid = int(key)
                draft = (state.get("drafts") or {}).get(key) or {}
                m = next((x for x in state["matches"] if x["publication_id"] == pid), {})
                action = item["action"]
                status = {"approve": "approved", "edit": "edited", "reject": "rejected"}[action]
                final_text = (
                    item.get("text") if action == "edit" and item.get("text") else draft.get("text")
                )
                conn.execute(
                    "UPDATE drafts SET status = %s WHERE run_id = %s AND publication_id = %s AND "
                    "version = 1",
                    (status, run_id, pid),
                )
                if action == "edit" and item.get("text"):
                    conn.execute(
                        "INSERT INTO drafts (run_id, tr_id, publication_id, version, text, "
                        "stance, status) "
                        "VALUES (%s, %s, %s, 2, %s, %s, 'edited') ON CONFLICT DO NOTHING",
                        (run_id, tr_id, pid, plain_dashes(item["text"]), draft.get("stance")),
                    )
                    memory.remember(
                        conn,
                        tr_id,
                        "draft_edit",
                        {
                            "machine_text": draft.get("text"),
                            "edited_text": plain_dashes(item["text"]),
                            "note": item.get("note"),
                        },
                        publication_id=pid,
                        run_id=run_id,
                    )
                    written += 1
                memory.remember(
                    conn,
                    tr_id,
                    status,
                    {"note": item.get("note"), "decided_by": review.get("decided_by")},
                    publication_id=pid,
                    run_id=run_id,
                )
                written += 1
                if action in ("approve", "edit"):
                    memory.remember(
                        conn,
                        tr_id,
                        "position",
                        {
                            "initiative_id": m.get("initiative_id"),
                            "stance": draft.get("stance"),
                            "text": final_text,
                        },
                        publication_id=pid,
                        run_id=run_id,
                    )
                    written += 1
            conn.commit()
            return (
                {"finished": True},
                f"{len(go.get('go', []))} go, {len(review.get('items', {}))} reviewed",
                f"{written} memory rows",
                None,
            )

        return self._run(state, "memory", work, tool="org_memory")


# --------------------------------------------------------------------- prompts and decisions


def _draft_prompt(profile, preferences, m, reading, landscape, limit: int) -> str:
    parts = [
        f"Organisation: {profile.get('name')} ({profile.get('acronym') or ''}), "
        f"{profile.get('category')}.",
        f"Goals as registered: {profile.get('goals') or ''}",
        f"Interests: {', '.join(profile.get('interests') or [])}.",
        f"Interest represented: {profile.get('interest_represented') or ''}.",
    ]
    for s in (profile.get("past_submissions") or [])[:VOICE_SUBMISSIONS]:
        if s.get("excerpt"):
            parts.append(f"Past submission ({s.get('title')}), for the voice:\n{s['excerpt']}")
    for p in preferences:
        parts.append(
            "Reviewer preference from an earlier draft (machine text, then the reviewer's "
            "version):\n"
            f"{(p.get('machine_text') or '')[:600]}\n---\n{(p.get('edited_text') or '')[:600]}"
        )
    parts.append(f"Consultation: {m.get('short_title')}\nPublication: {m.get('title')}")
    if reading.get("summary"):
        s = reading["summary"]
        parts.append(
            f"Summary of the proposal: {s.get('one_paragraph', '')}\nKey points: "
            + " | ".join(s.get("key_points", []))
        )
    if reading.get("chunks"):
        parts.append("Proposal passages (cite the chunk id you rely on):")
        parts.extend(f"[chunk:{c['id']} p.{c['page_no']}] {c['text']}" for c in reading["chunks"])
    if landscape.get("stances"):
        parts.append(f"Stances of submissions so far (counts): {landscape['stances']}")
    parts.append(
        f"Write the response text (at most {limit - 200} characters, plain paragraphs, "
        "no headings), "
        "state the stance (supports, opposes, wants_changes, unclear) and list chunk_ids_used."
    )
    return "\n\n".join(parts)


def _voice(profile) -> list[dict[str, Any]]:
    """The past submissions the drafter saw for the voice; the critic may cite them."""
    return [
        s
        for s in (profile.get("past_submissions") or [])[:VOICE_SUBMISSIONS]
        if s.get("excerpt") and s.get("feedback_id") is not None
    ]


def _critic_prompt(
    profile, sentences: list[str], chunks: list[dict], voice: list[dict] | None = None
) -> str:
    parts = [
        f"Organisation profile: {profile.get('name')}, {profile.get('category')}. Goals: "
        f"{(profile.get('goals') or '')[:800]}. "
        f"Interests: {', '.join(profile.get('interests') or [])}.",
    ]
    for v in voice or []:
        parts.append(
            f"[submission:{v['feedback_id']}] The organisation's own past submission "
            f"({v.get('title')}): {v['excerpt']}"
        )
    parts.append("Commission passages:")
    parts.extend(f"[chunk:{c['id']}] {c['text']}" for c in chunks)
    parts.append("Draft sentences:")
    parts.extend(f"({i}) {s}" for i, s in enumerate(sentences))
    parts.append(
        "Return JSON: sentences, one entry per index with supported, source and a short note."
    )
    return "\n".join(parts)


def _ids(value) -> list[int]:
    if not value:
        return []
    return [int(v) for v in value]


def _normalise_go(decision: Any, candidates: list[int]) -> dict[str, Any]:
    if not isinstance(decision, dict):
        raise ValueError("go/no-go decision must be a dict with go, no_go, not_our_topic lists")
    allowed = set(candidates)
    go = [p for p in _ids(decision.get("go")) if p in allowed]
    not_ours = [p for p in _ids(decision.get("not_our_topic")) if p in allowed and p not in go]
    no_go = [
        p for p in _ids(decision.get("no_go")) if p in allowed and p not in go and p not in not_ours
    ]
    for p in candidates:
        if p not in go and p not in no_go and p not in not_ours:
            no_go.append(p)
    return {
        "go": go,
        "no_go": no_go,
        "not_our_topic": not_ours,
        "decided_by": decision.get("decided_by"),
    }


def _normalise_review(decision: Any, keys: list[str]) -> dict[str, Any]:
    if not isinstance(decision, dict):
        raise ValueError("review decision must be a dict with items")
    items_in = decision.get("items") or {}
    items: dict[str, dict[str, Any]] = {}
    for key in keys:
        item = items_in.get(key) or items_in.get(int(key)) or {}
        action = item.get("action", "reject")
        if action not in ("approve", "edit", "reject"):
            raise ValueError(f"unknown action {action!r} for {key}")
        if action == "edit" and not item.get("text"):
            raise ValueError(f"edit for {key} needs the edited text")
        items[key] = {"action": action, "text": item.get("text"), "note": item.get("note")}
    return {"items": items, "decided_by": decision.get("decided_by")}
