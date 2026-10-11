"""Celery app: routes, queue position list (Redis)."""

from __future__ import annotations

import pytest

from inspraak import queue


def test_tasks_and_routes():
    conf = queue.celery_app.conf
    assert conf.task_routes["inspraak.run_brief"]["queue"] == "brief"
    assert conf.task_routes["inspraak.nightly_snapshot"]["queue"] == "nightly"
    assert not conf.beat_schedule  # no built-in schedule: the nightly tasks are sent by hand
    assert conf.worker_prefetch_multiplier == 1 and conf.task_acks_late is True
    assert {
        "inspraak.run_brief",
        "inspraak.resume_brief",
        "inspraak.precompute_publications",
    } <= set(queue.celery_app.tasks)


def test_queue_position_list():
    try:
        client = queue._redis()
        client.ping()
    except Exception:
        pytest.skip("Redis not reachable; start it with `make up`")
    client.delete(queue.QUEUE_KEY)
    client.rpush(queue.QUEUE_KEY, "run-a", "run-b", "run-c")
    assert queue.queue_position("run-a", client) == 1
    assert queue.queue_position("run-c", client) == 3
    assert queue.queue_position("nope", client) is None
    assert queue.queue_length(client) == 3
    queue.leave_queue("run-a", client)
    assert queue.queue_position("run-b", client) == 1
    client.delete(queue.QUEUE_KEY)
