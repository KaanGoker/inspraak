"""Celery app: the `brief` queue for organisation runs (with a visible queue position kept
in a Redis list), the `nightly` queue for the snapshot and the publication-layer
precompute, and the beat schedule. The model rate limit is not a Celery setting: every
process shares the Redis token bucket in `inspraak.llm.bucket`.

    uv run celery -A inspraak.queue worker -Q brief,nightly -l info
    uv run celery -A inspraak.queue beat -l info
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.schedules import crontab

from inspraak.config import get_settings

settings = get_settings()

celery_app = Celery("inspraak", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_default_queue="brief",
    task_routes={
        "inspraak.run_brief": {"queue": "brief"},
        "inspraak.resume_brief": {"queue": "brief"},
        "inspraak.nightly_snapshot": {"queue": "nightly"},
        "inspraak.precompute_publications": {"queue": "nightly"},
    },
    task_acks_late=True,
    worker_prefetch_multiplier=1,  # one brief at a time per worker, so the position is honest
    task_track_started=True,
    timezone=settings.timezone,
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "nightly-snapshot": {
            "task": "inspraak.nightly_snapshot",
            "schedule": crontab(hour=2, minute=30),
            "options": {"queue": "nightly"},
        },
        "precompute-publications": {
            "task": "inspraak.precompute_publications",
            "schedule": crontab(hour=4, minute=30),
            "options": {"queue": "nightly"},
        },
    },
)

QUEUE_KEY = "inspraak:brief:queue"


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=5)


# --------------------------------------------------------------------------- queue position


def enqueue_run(run_id: str) -> int:
    """Register the run in the position list, then send the task. Returns the position."""
    r = _redis()
    r.rpush(QUEUE_KEY, run_id)
    run_brief.apply_async(args=[run_id], queue="brief")
    return queue_position(run_id, client=r) or 1


def enqueue_resume(run_id: str, decision: dict[str, Any]) -> int:
    r = _redis()
    r.rpush(QUEUE_KEY, run_id)
    resume_brief.apply_async(args=[run_id, decision], queue="brief")
    return queue_position(run_id, client=r) or 1


def queue_position(run_id: str, client=None) -> int | None:
    """1-based position among the runs waiting for a worker, None when not queued."""
    r = client or _redis()
    pos = r.lpos(QUEUE_KEY, run_id)
    return None if pos is None else int(pos) + 1


def leave_queue(run_id: str, client=None) -> None:
    (client or _redis()).lrem(QUEUE_KEY, 1, run_id)


def queue_length(client=None) -> int:
    return int((client or _redis()).llen(QUEUE_KEY))


# --------------------------------------------------------------------------- tasks


@celery_app.task(name="inspraak.run_brief", bind=True)
def run_brief(self, run_id: str) -> dict[str, Any]:
    from inspraak.agents import service

    leave_queue(run_id)
    run = service.execute_run(run_id)
    return {"run_id": run_id, "status": run["status"], "stage": run["stage"]}


@celery_app.task(name="inspraak.resume_brief", bind=True)
def resume_brief(self, run_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    from inspraak.agents import service

    leave_queue(run_id)
    run = service.resume_run(run_id, decision)
    return {"run_id": run_id, "status": run["status"], "stage": run["stage"]}


@celery_app.task(name="inspraak.nightly_snapshot")
def nightly_snapshot(download: bool = True) -> dict[str, Any]:
    """The data layer's `inspraak ingest all`, from the worker."""
    from inspraak.cli import ingest_all

    ingest_all(download=download)
    return {"ok": True}


@celery_app.task(name="inspraak.precompute_publications", rate_limit="30/m")
def precompute_publications(
    publication_ids: list[int] | None = None, feedback_limit: int | None = None
) -> dict[str, Any]:
    """Reader summaries, feedback analyses and meeting maps for the in-scope publications.
    Stops cleanly when the daily budget is spent."""
    from inspraak import db
    from inspraak.jobs import precompute_publication, publications_to_precompute
    from inspraak.llm import get_provider

    provider = get_provider()
    out: dict[str, Any] = {"publications": 0, "calls": 0, "stopped": None}
    with db.session(settings.database_url) as conn:
        ids = publication_ids or publications_to_precompute(conn)
        for pid in ids:
            stats = precompute_publication(conn, provider, pid, feedback_limit=feedback_limit)
            out["publications"] += 1
            out["calls"] += stats["calls"]
            if stats.get("stopped"):
                out["stopped"] = stats["stopped"]
                break
    return out
