"""Structured outputs the model must return. One pydantic model per purpose."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Stance = Literal["supports", "opposes", "wants_changes", "unclear"]


class PublicationSummary(BaseModel):
    """Reader: what a Commission publication proposes, in plain words."""

    one_paragraph: str = Field(description="What the text proposes, 3 to 5 sentences.")
    key_points: list[str] = Field(default_factory=list, description="At most 6 concrete points.")
    affected_groups: list[str] = Field(
        default_factory=list, description="Who is bound or affected, as named in the text."
    )
    what_is_asked: str = Field(
        default="", description="What the Commission asks respondents to comment on."
    )


class KeyAsk(BaseModel):
    ask: str = Field(description="One demand or recommendation, one sentence.")
    quote: str = Field(description="A verbatim passage of the source text, copied exactly.")


class FeedbackAnalysis(BaseModel):
    """Feedback analyst: stance of one organisation submission toward the proposal."""

    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    key_asks: list[KeyAsk] = Field(default_factory=list, description="At most 3.")


class BatchItemAnalysis(FeedbackAnalysis):
    """One submission inside a batched analyst call."""

    submission: int = Field(description="The submission number given in the prompt.")
    opening_words: str = Field(
        description="The first 8 words of that submission's text, copied exactly."
    )


class FeedbackBatchAnalysis(BaseModel):
    """Feedback analyst, batched: one entry per submission in the prompt."""

    items: list[BatchItemAnalysis]


class DraftResponse(BaseModel):
    """Drafter: a Have Your Say response, at most 4,000 characters."""

    text: str
    stance: Stance
    chunk_ids_used: list[int] = Field(
        default_factory=list, description="ids of the proposal chunks the text relies on."
    )


class SentenceCheck(BaseModel):
    index: int
    supported: bool
    source: str = Field(
        description="'chunk:<id>' for a Commission passage, 'submission:<id>' for one of the "
        "organisation's past submissions, 'profile' for its register profile, 'none' when "
        "nothing supports the sentence. Several sources are separated by commas."
    )
    note: str = ""


class CriticReport(BaseModel):
    """Critic: every sentence of the draft, supported or not."""

    sentences: list[SentenceCheck]


class RerankItem(BaseModel):
    publication: int = Field(description="The consultation id given in the prompt.")
    relevance: float = Field(
        ge=0.0,
        le=1.0,
        description="1 = the subject sits inside the organisation's stated mission, 0 = unrelated.",
    )
    reason: str = Field(description="One short sentence: why it fits or why not.")


class RerankBatch(BaseModel):
    """Matcher rerank: one judgment per candidate consultation."""

    items: list[RerankItem]
