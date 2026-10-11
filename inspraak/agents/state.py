"""Graph state of one organisation-layer run. Every value is JSON-friendly so the
Postgres checkpointer can store it and the web stage can show it as is."""

from __future__ import annotations

import operator
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, TypedDict
from uuid import UUID


class RunState(TypedDict, total=False):
    run_id: str
    tr_id: str
    options: dict[str, Any]
    profile: dict[str, Any]
    matches: list[dict[str, Any]]
    landscape: dict[str, Any]  # keyed by str(publication_id)
    lobby: dict[str, Any]
    reading: dict[str, Any]
    go_decision: dict[str, Any]
    drafts: dict[str, Any]
    critiques: dict[str, Any]
    review_decision: dict[str, Any]
    escalations: Annotated[list[dict[str, Any]], operator.add]
    next: str
    finished: bool


WORKERS = (
    "profiler",
    "matcher",
    "landscape",
    "lobby",
    "reader",
    "go_no_go",
    "drafter",
    "critic",
    "review",
    "memory",
)


def jsonable(value: Any) -> Any:
    """Rows from psycopg into plain JSON values. Vectors are dropped."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items() if not hasattr(v, "to_numpy")}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "tolist"):
        return None
    return value
