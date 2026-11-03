"""FastAPI application: pages, HTMX partials and the review actions.

    uv run inspraak web            # uvicorn on port 8010
    uv run uvicorn inspraak.web.app:app --port 8010

Pages read the local snapshot and the agent tables only. The three write actions (start a
run, decide a review item, correct a profile) go through `inspraak.agents.service`, so a
decision on a blocking item resumes the graph (inline or through the Celery brief queue).
"""

from __future__ import annotations

import base64
import hmac
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from inspraak import db
from inspraak.agents import service
from inspraak.agents.service import RunNotFound
from inspraak.config import Settings, get_settings
from inspraak.textutil import plain_dashes
from inspraak.web import views
from inspraak.web.render import STATIC, Renderer


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    renderer = Renderer("app")
    app = FastAPI(title="Inspraak", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # ------------------------------------------------------------------ guards
    # With REVIEW_TOKEN set, every path but the static files and the health check asks
    # for the token: the app shows drafts, decisions and reviewer notes on its pages, so
    # reading is as private as writing. Writes also start model calls (runs, go
    # decisions) or change what later prompts read (profile edits), so they get a
    # same-origin check against cross-site form posts and a per-client rate limit.
    recent: dict[str, deque[float]] = defaultdict(deque)
    OPEN_PATHS = ("/healthz",)
    OPEN_PREFIXES = ("/static/",)

    def _authorised(request: Request) -> bool:
        if not settings.review_token:
            return True
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            _, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(password, settings.review_token)

    def _same_origin(request: Request) -> bool:
        source = request.headers.get("origin") or request.headers.get("referer")
        if not source or source == "null":
            return source is None  # no header: not a browser form post (curl, tests)
        return urlsplit(source).netloc == request.headers.get("host", "")

    def _rate_ok(request: Request) -> bool:
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        q = recent[client]
        while q and now - q[0] > 600:
            q.popleft()
        if len(q) >= settings.write_rate_per_10min:
            return False
        q.append(now)
        return True

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        writes = request.method == "POST"
        if path not in OPEN_PATHS and not path.startswith(OPEN_PREFIXES):
            if not _authorised(request):
                return Response(
                    "review token required",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="inspraak review"'},
                )
        if writes:
            if not _same_origin(request):
                return Response("cross-origin form post refused", status_code=403)
            if not _rate_ok(request):
                return Response("too many write actions, try again later", status_code=429)
        return await call_next(request)

    snapshot_cache: dict[str, Any] = {"at": 0.0, "value": None}

    def snapshot_date() -> str | None:
        """The date of the last Have Your Say ingest, for the footer; read once a minute."""
        if time.monotonic() - snapshot_cache["at"] > 60:
            try:
                with db.session(settings.database_url) as conn:
                    snapshot_cache["value"] = views.snapshot_date(conn)
            except Exception:  # the footer must never break a page
                snapshot_cache["value"] = None
            snapshot_cache["at"] = time.monotonic()
        return snapshot_cache["value"]

    def page(template: str, request: Request, **context: Any) -> HTMLResponse:
        context.setdefault("snapshot_date", snapshot_date())
        return HTMLResponse(renderer.render(template, request=request, **context))

    def is_htmx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        with db.session(settings.database_url) as conn:
            data = views.home(conn)
        return page("home.html", request, **data, title="Inspraak")

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = ""):
        with db.session(settings.database_url) as conn:
            results = views.search(conn, q)
        template = "partials/search_results.html" if is_htmx(request) else "search.html"
        return page(template, request, results=results, q=q, title=f"Search: {q}")

    @app.get("/org/{tr_id}", response_class=HTMLResponse)
    def organisation(request: Request, tr_id: str):
        with db.session(settings.database_url) as conn:
            data = views.organisation(conn, tr_id, settings)
        if data is None:
            raise HTTPException(404, f"organisation {tr_id} is not in the snapshot")
        return page("organisation.html", request, **data, title=data["profile"]["name"])

    @app.get("/consultation/{publication_id}", response_class=HTMLResponse)
    def consultation(request: Request, publication_id: int):
        with db.session(settings.database_url) as conn:
            data = views.consultation(conn, publication_id)
        if data is None:
            raise HTTPException(404, f"publication {publication_id} is not in the snapshot")
        return page(
            "consultation.html",
            request,
            **data,
            title=data["publication"]["short_title"] or data["publication"]["title"],
        )

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request):
        with db.session(settings.database_url) as conn:
            data = views.inbox(conn)
        return page("inbox.html", request, **data, title="Review inbox")

    @app.get("/run/{run_id}", response_class=HTMLResponse)
    def run(request: Request, run_id: str):
        try:
            data = views.run(run_id)
        except RunNotFound as exc:
            raise HTTPException(404, f"run {run_id} not found") from exc
        return page("run.html", request, run=data, title=f"Run for {data['organisation']}")

    @app.get("/run/{run_id}/status", response_class=HTMLResponse)
    def run_status(request: Request, run_id: str):
        try:
            data = views.run(run_id)
        except RunNotFound as exc:
            raise HTTPException(404, f"run {run_id} not found") from exc
        return page("partials/run_status.html", request, run=data)

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request):
        with db.session(settings.database_url) as conn:
            data = views.about(conn)
        return page("about.html", request, **data, title="How it works")

    @app.get("/healthz")
    def healthz():
        with db.session(settings.database_url) as conn:
            n = conn.execute("SELECT count(*) AS n FROM organisations").fetchone()["n"]
        return JSONResponse({"ok": True, "organisations": n})

    # ------------------------------------------------------------------ actions
    @app.post("/org/{tr_id}/run")
    async def start_run(request: Request, tr_id: str):
        form = await request.form()
        requested_by = (form.get("requested_by") or "web").strip()[:80]
        with db.session(settings.database_url) as conn:
            busy = conn.execute(
                "SELECT id FROM runs WHERE tr_id = %s AND status IN ('queued', 'running') LIMIT 1",
                (tr_id,),
            ).fetchone()
        if busy:  # one run in flight per organisation
            return RedirectResponse(f"/run/{busy['id']}", status_code=303)
        try:
            run = service.start_run(tr_id, requested_by=requested_by)
        except RunNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return RedirectResponse(f"/run/{run['id']}", status_code=303)

    @app.post("/org/{tr_id}/profile")
    async def edit_profile(request: Request, tr_id: str):
        form = await request.form()
        edits: dict[str, Any] = {}
        for field in ("goals", "interest_represented", "website", "note"):
            value = (form.get(field) or "").strip()
            if value:
                edits[field] = plain_dashes(value)
        interests = (form.get("interests") or "").strip()
        if interests:
            edits["interests"] = [
                plain_dashes(i.strip()) for i in interests.split(",") if i.strip()
            ]
        by = (form.get("by") or "web").strip()[:80]
        if edits:
            service.edit_profile(tr_id, edits, by=by)
        return RedirectResponse(f"/org/{tr_id}#profile", status_code=303)

    @app.post("/review/{item_id}", response_class=HTMLResponse)
    async def decide(request: Request, item_id: int):
        form = await request.form()
        with db.session(settings.database_url) as conn:
            item = views.inbox_item(conn, item_id)
        if item is None:
            raise HTTPException(404, f"review item {item_id} not found")
        if item["status"] == "decided":
            raise HTTPException(409, f"review item {item_id} is already decided")
        decision = decision_from_form(item, dict(form))
        decided_by = (form.get("decided_by") or "web").strip()[:80] or "web"
        try:
            out = service.decide(item_id, decision, decided_by=decided_by)
        except service.AlreadyDecided as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if item["kind"] == "relevance" and decision.get("action") == "not_our_topic":
            with db.session(settings.database_url) as conn:
                from inspraak.agents import memory

                memory.remember(
                    conn,
                    item["tr_id"],
                    "not_our_topic",
                    {"decided_by": decided_by, "from_review_item": item_id},
                    publication_id=item["publication_id"],
                    run_id=item.get("run_id"),
                )
        if is_htmx(request):
            with db.session(settings.database_url) as conn:
                fresh = views.inbox_item(conn, item_id)
            return page("partials/inbox_item.html", request, item=fresh, decided_now=out)
        return RedirectResponse("/inbox", status_code=303)

    return app


def decision_from_form(item: dict[str, Any], form: dict[str, Any]) -> dict[str, Any]:
    """Form fields of the inbox into the decision shape `service.decide` expects."""
    kind = item["kind"]
    if kind == "go_no_go":
        decision: dict[str, Any] = {"go": [], "no_go": [], "not_our_topic": []}
        for c in item["payload"].get("consultations", []):
            pid = int(c["publication_id"])
            choice = form.get(f"decision_{pid}", "no_go")
            decision.get(choice, decision["no_go"]).append(pid)
        return decision
    if kind == "draft_review":
        items: dict[str, Any] = {}
        for d in item["payload"].get("drafts", []):
            pid = str(d["publication_id"])
            action = form.get(f"action_{pid}", "reject")
            text = plain_dashes((form.get(f"text_{pid}") or "").strip())
            note = (form.get(f"note_{pid}") or "").strip() or None
            if action == "edit" and (not text or text == (d.get("text") or "").strip()):
                action = "approve"
            items[pid] = {
                "action": action,
                "text": text if action == "edit" else None,
                "note": note,
            }
        return {"items": items}
    action = form.get("action", "acknowledge")
    decision = {"action": action, "note": (form.get("note") or "").strip() or None}
    if kind == "stance" and form.get("stance"):
        decision["stance"] = form["stance"]
    return decision


app = create_app()
