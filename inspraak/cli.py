"""`inspraak` command line: data layer, model provider, precompute, runs, review, web, export."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer

from inspraak import db
from inspraak.config import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Inspraak command line.")
db_app = typer.Typer(no_args_is_help=True, help="Database.")
ingest_app = typer.Typer(no_args_is_help=True, help="Snapshots of the public sources.")
org_app = typer.Typer(no_args_is_help=True, help="Look up organisations in the snapshot.")
llm_app = typer.Typer(no_args_is_help=True, help="Model provider: probe and call counts.")
run_app = typer.Typer(no_args_is_help=True, help="Organisation-layer runs (the LangGraph graph).")
review_app = typer.Typer(no_args_is_help=True, help="Review inbox: list and decide.")
app.add_typer(db_app, name="db")
app.add_typer(ingest_app, name="ingest")
app.add_typer(org_app, name="org")
app.add_typer(llm_app, name="llm")
app.add_typer(run_app, name="run")
app.add_typer(review_app, name="review")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "tolist"):
        return "<vector>"
    return str(value)


def say(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default))


def log(message: str) -> None:
    typer.echo(f"[{datetime.now():%H:%M:%S}] {message}", err=True)


def _record_run(source: str, work) -> dict:
    """Run `work(conn)` inside an ingest_runs row so every load is on record."""
    with db.session() as conn:
        run_id = db.start_run(conn, source)
        try:
            stats = work(conn)
        except Exception as exc:
            db.finish_run(conn, run_id, False, {"error": f"{type(exc).__name__}: {exc}"})
            raise
        db.finish_run(conn, run_id, True, stats)
        return stats


# --------------------------------------------------------------------------- db


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending SQL migrations."""
    applied = db.migrate()
    say({"applied": applied})


@db_app.command("stats")
def db_stats() -> None:
    """Row counts of the snapshot."""
    from inspraak.store import snapshot_stats

    with db.session() as conn:
        say(snapshot_stats(conn))


@db_app.command("runs")
def db_runs(limit: int = 20) -> None:
    """Recent ingest runs with their stats."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, source, started_at, finished_at, ok, stats FROM ingest_runs "
            "ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    say(rows)


# --------------------------------------------------------------------------- ingest


@ingest_app.command("register")
def ingest_register(
    download: bool = typer.Option(True, help="Fetch the export; --no-download reuses the file."),
    path: Path | None = typer.Option(None, help="Use this XML file instead of the default."),
    prune: bool = typer.Option(True, help="Delete registrants missing from the export."),
) -> None:
    """EU Transparency Register XML export to `organisations`."""
    from inspraak.ingest.register import download_register, load_register

    settings = get_settings()
    xml_path = path or settings.raw_dir / "register" / "register.xml"
    if download:
        log("downloading the register export (about 117 MB)")
        xml_path = download_register(dest=xml_path)
        log(f"saved {xml_path} ({xml_path.stat().st_size} bytes)")

    def work(conn):
        return load_register(conn, xml_path, prune=prune)

    say(_record_run("register", work))


@ingest_app.command("meetings")
def ingest_meetings(
    download: bool = typer.Option(True, help="Fetch the XLSX; --no-download reuses the file."),
    path: Path | None = typer.Option(None),
) -> None:
    """Commission meetings with interest representatives (2024 to 2029) to `meetings`."""
    from inspraak.ingest.meetings import download_meetings, load_meetings

    settings = get_settings()
    xlsx_path = path or settings.raw_dir / "meetings" / "meetings2429.xlsx"
    if download:
        xlsx_path = download_meetings(dest=xlsx_path)
        log(f"saved {xlsx_path} ({xlsx_path.stat().st_size} bytes)")
    say(_record_run("meetings", lambda conn: load_meetings(conn, xlsx_path)))


@ingest_app.command("hys")
def ingest_hys(
    skip_closed: bool = typer.Option(False, help="Only the open publications."),
    cache_hours: float | None = typer.Option(None, help="Reuse cached pages younger than this."),
) -> None:
    """Have Your Say listing, details and feedback for the V1 scope. Resumable."""
    from inspraak.ingest.cache import RawCache
    from inspraak.ingest.hys import run_hys

    cache = RawCache(max_age_hours=cache_hours)
    say(
        _record_run(
            "hys", lambda conn: run_hys(conn, cache=cache, skip_closed=skip_closed, log=log)
        )
    )


@ingest_app.command("documents")
def ingest_documents(
    kind: str = typer.Option("proposal", help="proposal or feedback_attachment"),
    limit: int | None = typer.Option(None, help="Stop after this many documents."),
) -> None:
    """Download and extract pending documents of in-scope publications."""
    from inspraak.ingest.documents import run_documents

    if kind not in ("proposal", "feedback_attachment"):
        raise typer.BadParameter("kind must be proposal or feedback_attachment")
    say(
        _record_run(
            f"documents:{kind}", lambda conn: run_documents(conn, kind, limit=limit, log=log)
        )
    )


@ingest_app.command("all")
def ingest_all(download: bool = typer.Option(True)) -> None:
    """register, meetings, hys, documents (both kinds), embed. The nightly job."""
    ingest_register(download=download, path=None, prune=True)
    ingest_meetings(download=download, path=None)
    ingest_hys(skip_closed=False, cache_hours=None)
    ingest_documents(kind="proposal", limit=None)
    ingest_documents(kind="feedback_attachment", limit=None)
    embed(target=None, limit=None)


# --------------------------------------------------------------------------- embed


@app.command("embed")
def embed(
    target: str | None = typer.Option(
        None, help="organisations, publications, feedback, chunks or meetings; default all."
    ),
    limit: int | None = typer.Option(None, help="Rows per target, for a quick check."),
) -> None:
    """Compute the missing vectors with the local model."""
    from inspraak.embeddings import TARGETS, embed_all

    names = [target] if target else None
    if target and target not in TARGETS:
        raise typer.BadParameter(f"unknown target {target}, choose from {', '.join(TARGETS)}")
    say(_record_run("embed", lambda conn: embed_all(conn, names, limit=limit, log=log)))


# --------------------------------------------------------------------------- org


@org_app.command("search")
def org_search(query: str, limit: int = 10) -> None:
    """Find an organisation by name, acronym or register ID."""
    from inspraak.store import find_organisations

    with db.session() as conn:
        say(find_organisations(conn, query, limit=limit))


@org_app.command("show")
def org_show(tr_id: str) -> None:
    """Profile, past submissions and cabinet meetings of one registrant."""
    from inspraak.store import get_organisation, meetings_for_organisation, organisation_submissions

    with db.session() as conn:
        org = get_organisation(conn, tr_id)
        if org is None:
            raise typer.Exit(code=1)
        say(
            {
                "organisation": org,
                "submissions": organisation_submissions(conn, tr_id),
                "meetings": meetings_for_organisation(conn, tr_id),
            }
        )


@org_app.command("match")
def org_match(tr_id: str, limit: int = 10, open_only: bool = False) -> None:
    """In-scope consultations closest to the organisation's register profile (no LLM)."""
    from inspraak.store import organisation_vector, similar_publications

    with db.session() as conn:
        vector = organisation_vector(conn, tr_id)
        if vector is None:
            log("no profile vector yet: run `inspraak embed --target organisations`")
            raise typer.Exit(code=1)
        say(similar_publications(conn, vector, limit=limit, open_only=open_only))


@app.command("consultations")
def consultations(open_only: bool = False) -> None:
    """In-scope publications, sorted by deadline."""
    from inspraak.store import list_publications

    with db.session() as conn:
        say(list_publications(conn, open_only=open_only))


@app.command("eval-match")
def eval_match(
    rerank_sample: int = typer.Option(
        0, help="Also rerank this many sampled organisations with LLM_PROVIDER (model calls)."
    ),
    seed: int = typer.Option(42, help="Seed of the rerank sample."),
    workers: int = typer.Option(8, help="Parallel rerank calls."),
) -> None:
    """Match recall against real past submissions: embedding, hub penalty, per initiative,
    and optionally the model rerank on a sample (that part calls the model)."""
    from inspraak.evaluate import match_baseline, rerank_eval

    with db.session() as conn:
        result = match_baseline(conn)
    if rerank_sample:
        from inspraak.llm import make_provider

        provider = make_provider()
        result["rerank_sample"] = rerank_eval(
            provider, sample=rerank_sample, seed=seed, workers=workers, log=log
        )
        result["rerank_sample"]["provider"] = f"{provider.name} {provider.model}"
    say(result)


@app.command("match-calibrate")
def match_calibrate() -> None:
    """Recompute the hub penalty per publication (also done at the end of `embed`)."""
    from inspraak.agents.matcher import compute_hub_scores

    with db.session() as conn:
        say(compute_hub_scores(conn))


@app.command("rerank")
def rerank(
    file: Path = typer.Option(Path("demo_set.csv"), help="tr_id per line (csv with header)."),
    tr_id: list[str] = typer.Option([], help="Organisations instead of the file."),
    workers: int = typer.Option(8, help="Parallel organisations."),
) -> None:
    """Fill the rerank cache for organisations with LLM_PROVIDER: one call per
    organisation for the candidates that have no cached judgment yet."""
    import csv
    from concurrent.futures import ThreadPoolExecutor

    from inspraak.agents import memory
    from inspraak.agents.matcher import match
    from inspraak.agents.rerank import Reranker
    from inspraak.config import get_settings
    from inspraak.llm import make_provider
    from inspraak.store import get_organisation, organisation_vector

    ids = list(tr_id)
    if not ids:
        with file.open(encoding="utf-8") as fh:
            ids = [r["tr_id"] for r in csv.DictReader(fh) if r.get("tr_id")]
    provider = make_provider()
    settings = get_settings()

    def one(org_id: str) -> dict:
        with db.session() as conn:
            org = get_organisation(conn, org_id)
            vector = organisation_vector(conn, org_id)
            if org is None or vector is None:
                return {"tr_id": org_id, "skipped": "no profile or vector"}
            profile = dict(org)
            edits = memory.profile_edits(conn, org_id)
            profile.update(edits)
            reranker = Reranker(conn, org_id, profile, provider, log=log)
            matches = match(
                conn,
                vector,
                top_k=settings.match_top_k,
                min_score=settings.match_min_score,
                uncertain_score=settings.match_uncertain_score,
                corrections=memory.not_our_topic_ids(conn, org_id),
                reranker=reranker,
            )
        log(f"{org['name']}: {reranker.calls} calls, {reranker.cached} cached")
        return {
            "tr_id": org_id,
            "organisation": org["name"],
            "calls": reranker.calls,
            "cached": reranker.cached,
            "error": reranker.error,
            "matches": [(m["publication_id"], m.get("relevance"), m["score"]) for m in matches],
        }

    with ThreadPoolExecutor(max_workers=workers) as ex:
        out = list(ex.map(one, ids))
    say(
        {
            "provider": f"{provider.name} {provider.model}",
            "organisations": len(out),
            "calls": sum(o.get("calls", 0) for o in out),
            "errors": sum(1 for o in out if o.get("error")),
            "runs": out,
        }
    )


@app.command("eval-report")
def eval_report(
    provider: str | None = typer.Option("gemini", help="Narrow the call log and caches."),
    runs: bool = typer.Option(False, help="Include the per-run table."),
    write: Path | None = typer.Option(
        None,
        help="Write the headline numbers to this JSON file (docs/eval.json), keeping its "
        "hand-measured 'manual' block. The about page reads that file.",
    ),
) -> None:
    """The measured numbers behind docs/EVAL.md: quotes verified, calls per organisation,
    cache hit rate, timings, critic, inbox."""
    from inspraak.evaluate import headline, report

    if write is not None:
        from datetime import UTC, datetime

        with db.session() as conn:
            measured = headline(conn)
        old = json.loads(write.read_text(encoding="utf-8")) if write.exists() else {}
        doc = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "measured": measured,
            "manual": old.get("manual") or {},
        }
        write.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
        )
        say({"written": str(write), **measured})
        return

    with db.session() as conn:
        out = report(conn, provider=provider or None)
    if not runs:
        out["runs"].pop("per_run", None)
    say(out)


# --------------------------------------------------------------------------- llm


@llm_app.command("probe")
def llm_probe() -> None:
    """One tiny structured call with the configured provider (gemini: real quota)."""
    from inspraak.llm import make_provider

    provider = make_provider()
    if provider.name == "gemini":
        from inspraak.llm.gemini import probe

        say(probe(provider))
    else:
        from pydantic import BaseModel

        class Probe(BaseModel):
            ok: bool

        result = provider.complete_json("probe", Probe, purpose="probe")
        say(
            {"provider": provider.name, "model": provider.model, "answer": result.data.model_dump()}
        )


@llm_app.command("calls")
def llm_calls() -> None:
    """Real call counts and tokens from the llm_calls table, by provider, model and purpose."""
    from inspraak.agents.service import llm_call_counts

    say(llm_call_counts())


# --------------------------------------------------------------------------- precompute


@app.command("precompute")
def precompute(
    publication: list[int] = typer.Option(None, "--publication", "-p", help="Publication ids."),
    open_only: bool = typer.Option(
        False, help="Without ids: only the open publications (default: the whole V1 scope)."
    ),
    feedback_limit: int | None = typer.Option(
        None, help="Model calls for submissions per publication and pass (budget)."
    ),
) -> None:
    """Publication layer: Reader summary, Feedback analyst, Meetings mapper. Cached forever.

    Open publications first (nearest deadline first), then the recently closed ones.
    Stops cleanly when the daily budget is spent; run it again to continue."""
    from inspraak.jobs import precompute_publication, publications_to_precompute
    from inspraak.llm import make_provider

    provider = make_provider()
    started = datetime.now()
    with db.session() as conn:
        ids = publication or publications_to_precompute(conn, open_only=open_only)
        out = []
        stopped = None
        for n, pid in enumerate(ids, 1):
            log(f"publication {pid} ({n}/{len(ids)}, {provider.calls} calls so far)")
            stats = precompute_publication(
                conn, provider, pid, feedback_limit=feedback_limit, log=log
            )
            out.append(stats)
            if stats.get("stopped"):
                stopped = stats["stopped"]
                log(f"stopped: {stopped}; run `make precompute` again to continue")
                break
    say(
        {
            "provider": provider.name,
            "model": provider.model,
            "calls": provider.calls,
            "failed_calls": provider.failed_calls,
            "seconds": round((datetime.now() - started).total_seconds(), 1),
            "publications_done": len(out) - (1 if stopped else 0),
            "publications_total": len(ids),
            "stopped": stopped,
            "publications": out,
        }
    )


# --------------------------------------------------------------------------- run


@run_app.command("start")
def run_start(
    tr_id: str,
    inline: bool = typer.Option(True, help="--no-inline sends the run to the Celery brief queue."),
    requested_by: str = typer.Option("cli"),
    min_score: float | None = typer.Option(None, help="Matcher threshold override."),
    top_k: int | None = typer.Option(None),
    replace: bool = typer.Option(
        False, help="Rebuild runs that wait at an undecided go / no-go (new match list)."
    ),
) -> None:
    """Start an organisation run; stops at the go/no-go interrupt."""
    from inspraak.agents.service import start_run

    options = {k: v for k, v in {"min_score": min_score, "top_k": top_k}.items() if v is not None}
    say(start_run(tr_id, requested_by=requested_by, options=options, inline=inline))


@app.command("demo")
def demo(
    file: Path = typer.Option(Path("demo_set.csv"), help="tr_id,name,group per line."),
    top_k: int | None = typer.Option(None),
    replace: bool = typer.Option(
        False, help="Rebuild runs that wait at an undecided go / no-go (new match list)."
    ),
) -> None:
    """Brief stage for the demo set: one run per listed organisation that has none yet.
    Resumable; each run stops at the go / no-go interrupt. Costs one shared call per
    matched publication without a cached summary, else none."""
    import csv

    from inspraak.agents.service import start_demo
    from inspraak.llm import make_provider

    with file.open(encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("tr_id")]
    provider = make_provider()
    options = {"top_k": top_k} if top_k else None
    out = start_demo(
        [r["tr_id"] for r in rows], options=options, provider=provider, log=log, replace=replace
    )
    say(
        {
            "provider": provider.name,
            "model": provider.model,
            "calls": provider.calls,
            "listed": len(rows),
            "started": sum(1 for o in out if "run_id" in o and not o.get("skipped")),
            "skipped": sum(1 for o in out if o.get("skipped")),
            "replaced": sum(1 for o in out if o.get("replaced")),
            "errors": sum(1 for o in out if o.get("error")),
            "stopped": next((o["stopped"] for o in out if o.get("stopped")), None),
            "runs": out,
        }
    )


@app.command("demo-seed")
def demo_seed(
    force: bool = typer.Option(False, help="Empty a non-empty snapshot first (destructive)."),
) -> None:
    """Load the small fixture snapshot the tests use, precompute it with the deterministic
    provider and start one run, so the app has pages to show without the full ingest.
    Refuses to touch a database that already holds a snapshot unless --force is given."""
    from inspraak.seed import clear_snapshot, seed_demo, snapshot_is_empty

    with db.session() as conn:
        if not snapshot_is_empty(conn):
            if not force:
                log("the database already holds a snapshot; pass --force to replace it")
                raise typer.Exit(code=1)
            clear_snapshot(conn)
        say(seed_demo(conn, log=log))


@run_app.command("show")
def run_show(run_id: str) -> None:
    """Run row, trace, review items, drafts and call counts."""
    from inspraak.agents.service import get_run

    say(get_run(run_id))


@run_app.command("retry")
def run_retry(run_id: str) -> None:
    """Continue a failed run from its last checkpoint."""
    from inspraak.agents.service import retry_run

    say(retry_run(run_id))


@run_app.command("list")
def run_list(tr_id: str | None = None, limit: int = 20) -> None:
    from inspraak.agents.service import list_runs

    say(list_runs(tr_id, limit=limit))


@run_app.command("graph")
def run_graph() -> None:
    """The graph as mermaid, for docs/ARCHITECTURE.md."""
    from inspraak.agents.graph import mermaid

    typer.echo(mermaid())


# --------------------------------------------------------------------------- review


@review_app.command("list")
def review_list(
    status: str = typer.Option("open"),
    tr_id: str | None = None,
    run_id: str | None = None,
    kind: str | None = None,
) -> None:
    from inspraak.agents.service import list_review_items

    say(list_review_items(status or None, tr_id=tr_id, run_id=run_id, kind=kind))


@review_app.command("decide")
def review_decide(
    item_id: int,
    decision: str = typer.Argument(
        ...,
        help='JSON, e.g. \'{"go": [25981]}\' or \'{"items": {"25981": {"action": "approve"}}}\'',
    ),
    decided_by: str = typer.Option("cli"),
    inline: bool = typer.Option(True, help="--no-inline resumes through the Celery brief queue."),
) -> None:
    """Decide a review item; a blocking item resumes the run."""
    from inspraak.agents.service import decide

    say(decide(item_id, json.loads(decision), decided_by=decided_by, inline=inline))


@review_app.command("edit-profile")
def review_edit_profile(tr_id: str, edits: str, by: str = typer.Option("cli")) -> None:
    """Store reviewer corrections to the register profile (JSON object)."""
    from inspraak.agents.service import edit_profile

    say({"memory_id": edit_profile(tr_id, json.loads(edits), by=by)})


# --------------------------------------------------------------------------- web and export


@app.command("web")
def web(
    host: str = typer.Option("127.0.0.1", help="0.0.0.0 inside docker compose."),
    port: int = typer.Option(8010),
    reload: bool = typer.Option(False, help="Restart on code changes (development)."),
) -> None:
    """Serve the interactive app (FastAPI, Jinja2, HTMX)."""
    import uvicorn

    uvicorn.run("inspraak.web.app:app", host=host, port=port, reload=reload)


@app.command("export")
def export(
    out: Path = typer.Option(Path("site"), help="Output folder (created, files overwritten)."),
    org: list[str] = typer.Option(
        None, "--org", help="Register IDs to export; default: every organisation with a run."
    ),
) -> None:
    """Static site: precomputed organisation and consultation pages, relative links."""
    from inspraak.export import export_site

    say(export_site(out, organisations=org or None, log=log))


if __name__ == "__main__":
    app()
