"""The organisation-layer graph.

    START -> supervisor -> (profiler | matcher | landscape | lobby | reader | go_no_go |
             drafter | critic | review | memory) -> supervisor ... memory -> END

The supervisor routes deterministically on what the state already holds (see
`inspraak.agents.nodes.route`). `go_no_go` and `review` call `interrupt()`; the run waits
in the Postgres checkpointer until `service.decide` resumes it with the decision.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row

from inspraak.agents.nodes import Nodes
from inspraak.agents.state import WORKERS, RunState
from inspraak.config import Settings, get_settings
from inspraak.llm.base import Provider


def build_graph(provider: Provider, checkpointer=None, settings: Settings | None = None):
    nodes = Nodes(provider, settings or get_settings())
    graph = StateGraph(RunState)
    graph.add_node("supervisor", nodes.supervisor)
    for name in WORKERS:
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", lambda s: s["next"], {n: n for n in WORKERS})
    for name in WORKERS:
        if name != "memory":
            graph.add_edge(name, "supervisor")
    graph.add_edge("memory", END)
    return graph.compile(checkpointer=checkpointer)


@contextmanager
def open_checkpointer(database_url: str | None = None) -> Iterator[PostgresSaver]:
    """A PostgresSaver on its own autocommit connection; `setup()` is idempotent."""
    url = database_url or get_settings().database_url
    with psycopg.connect(url, autocommit=True, row_factory=dict_row, prepare_threshold=0) as conn:
        saver = PostgresSaver(conn)
        saver.setup()
        yield saver


def mermaid(provider: Provider | None = None) -> str:
    from inspraak.llm.fake import FakeProvider

    return build_graph(provider or FakeProvider()).get_graph().draw_mermaid()
