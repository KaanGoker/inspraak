"""Static export: the precomputed organisation and consultation pages as plain files.

    inspraak export --out site/

Layout (relative links only, so it serves from any path, GitHub Pages included):

    site/index.html             home with a search over search.json
    site/about.html
    site/disclaimer.html
    site/search.json            [{tr_id, name, acronym, category, country, url}]
    site/org/<tr_id>.html       organisations with at least one run (or --org ...)
    site/consultation/<id>.html every in-scope publication
    site/run/<run_id>.html      the brief stage of each exported organisation's latest run
    site/static/style.css
    site/.nojekyll

The export never calls a model and never writes to the database: it renders the same view
models as the app in static mode. The public site leaves out what belongs to the people
using the app: drafts, go / no-go decisions and who made them, the memory log, review
items, model confidence numbers, and every run step after the pause for the human go /
no-go. Organisation pages carry a robots noindex.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

from inspraak import db
from inspraak.config import Settings, get_settings
from inspraak.textutil import plain_dashes
from inspraak.web import links, views
from inspraak.web.render import STATIC, Renderer

Log = Callable[[str], None]


def export_site(
    out: Path,
    *,
    organisations: list[str] | None = None,
    settings: Settings | None = None,
    log: Log | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    say = log or (lambda _msg: None)
    renderer = Renderer("static")
    out = Path(out)
    if out.exists():
        # a stale page from an earlier export (a draft, a run) must not survive
        for sub in ("org", "consultation", "run"):
            shutil.rmtree(out / sub, ignore_errors=True)
    for sub in ("org", "consultation", "run", "static"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    stats: dict[str, Any] = {"out": str(out), "pages": 0, "bytes": 0}

    def write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        stats["pages"] += 1
        stats["bytes"] += len(text.encode("utf-8"))

    with db.session(settings.database_url) as conn:
        renderer.snapshot_date = views.snapshot_date(conn)
        tr_ids = organisations or [o["tr_id"] for o in views.demo_organisations(conn)]
        tr_ids = sorted(set(tr_ids))

        # home and about
        home = views.home(conn)
        home["demo_organisations"] = [
            o for o in home["demo_organisations"] if o["tr_id"] in set(tr_ids)
        ]
        write(out / "index.html", renderer.render("home.html", title="Inspraak", **home))
        write(
            out / "about.html",
            renderer.render("about.html", title="How it works", **views.about(conn)),
        )
        write(
            out / "disclaimer.html",
            renderer.render("disclaimer.html", title="Disclaimer", **views.about(conn)),
        )

        # organisations and their runs
        index: list[dict[str, Any]] = []
        run_ids: list[str] = []
        for tr_id in tr_ids:
            data = views.organisation(conn, tr_id, settings)
            if data is None:
                say(f"skip {tr_id}: not in the snapshot")
                continue
            data = public_organisation(conn, data)
            write(
                out / "org" / f"{tr_id}.html",
                renderer.render(
                    "organisation.html", rel="../", title=data["profile"]["name"], **data
                ),
            )
            p = data["profile"]
            index.append(
                {
                    "tr_id": tr_id,
                    "name": p["name"],
                    "acronym": p.get("acronym"),
                    "category": p.get("category"),
                    "country": (p.get("head_country") or "").title(),
                    "url": f"org/{tr_id}.html",
                }
            )
            run_ids.extend(r["id"] for r in data["runs"])
            say(
                f"org {tr_id}: {len(data['consultations'])} consultations, {len(data['runs'])} runs"
            )
        stats["organisations"] = len(index)

        # every in-scope publication
        pubs = _in_scope_publication_ids(conn)
        for pid in pubs:
            data = views.consultation(conn, pid)
            if data is None:
                continue
            title = data["publication"]["short_title"] or data["publication"]["title"]
            write(
                out / "consultation" / f"{pid}.html",
                renderer.render("consultation.html", rel="../", title=title, **data),
            )
        stats["consultations"] = len(pubs)

        for run_id in sorted(set(run_ids)):
            data = views.public_run(views.run(run_id))
            write(
                out / "run" / f"{run_id}.html",
                renderer.render(
                    "run.html", rel="../", title=f"Run for {data['organisation']}", run=data
                ),
            )
        stats["runs"] = len(set(run_ids))

    (out / "search.json").write_text(
        plain_dashes(json.dumps(index, ensure_ascii=False)), encoding="utf-8"
    )
    (out / ".nojekyll").write_text("", encoding="utf-8")
    for name in ("style.css", "mark.svg", "favicon.png", "og.png"):
        shutil.copy2(STATIC / name, out / "static" / name)
    stats["links_unlinked"] = unlink_missing_pages(out)
    stats["bytes"] = sum(p.stat().st_size for p in out.rglob("*.html"))
    stats["files"] = sum(1 for p in out.rglob("*") if p.is_file())
    return stats


def public_organisation(conn: psycopg.Connection, data: dict[str, Any]) -> dict[str, Any]:
    """The organisation view model without what stays in the app: cards kept only because
    of a draft, the draft and decision of every card, the memory log, and every run but
    the latest current one (its brief stage is exported)."""
    out = dict(data)
    consultations = []
    for c in data["consultations"]:
        if c.get("kept"):
            continue
        c = dict(c)
        for key in ("draft", "decision", "decision_label", "decision_is_demo"):
            c[key] = None
        consultations.append(c)
    out["consultations"] = consultations
    out["memory"] = []
    out["open_review_items"] = 0
    out["drafts_total"] = 0
    keep = set(views.exported_run_ids(conn, data["profile"]["tr_id"]))
    out["runs"] = [r for r in data["runs"] if str(r["id"]) in keep]
    return out


_INTERNAL_LINK = re.compile(
    r'<a\b[^>]*\bhref="((?:\.\./)?(org|consultation|run)/([^"/]+)\.html)(#[^"]*)?"[^>]*>'
    r"(.*?)</a>",
    re.DOTALL,
)


def unlink_missing_pages(out: Path) -> int:
    """Only a subset of organisations is exported, so a link to any other organisation (or to
    a publication outside the scope) would be a 404 on the static site. Such a link becomes
    the register entry for an organisation, plain text otherwise. Returns how many."""
    changed = 0
    for page in out.rglob("*.html"):
        html = page.read_text(encoding="utf-8")

        def fix(m: re.Match, base: Path = page.parent) -> str:
            nonlocal changed
            href, kind, ident, _anchor, inner = m.groups()
            if (base / href).resolve().is_file():
                return m.group(0)
            changed += 1
            if kind == "org":
                return (
                    f'<a href="{links.organisation(ident)}" rel="noopener" '
                    f'title="Transparency Register entry (no page in this demo)">{inner}</a>'
                )
            return f'<span title="no page in this demo">{inner}</span>'

        new = _INTERNAL_LINK.sub(fix, html)
        if new != html:
            page.write_text(new, encoding="utf-8")
    return changed


def _in_scope_publication_ids(conn: psycopg.Connection) -> list[int]:
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM publications WHERE in_scope ORDER BY end_date, id"
        ).fetchall()
    ]
