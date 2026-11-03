"""One Jinja2 environment for the app and the static export. The `u()` helper in every
template builds an app path (`/org/<id>`) or a relative file path (`../org/<id>.html`)
depending on the mode, so the same templates serve both."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from inspraak.config import get_settings, portal_now
from inspraak.textutil import plain_dashes

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

APP_PATHS = {
    "home": "/",
    "about": "/about",
    "disclaimer": "/disclaimer",
    "inbox": "/inbox",
    "search": "/search",
    "org": "/org/{id}",
    "consultation": "/consultation/{id}",
    "run": "/run/{id}",
    "static": "/static/{id}",
}
STATIC_PATHS = {
    "home": "index.html",
    "about": "about.html",
    "disclaimer": "disclaimer.html",
    "inbox": None,
    "search": None,
    "org": "org/{id}.html",
    "consultation": "consultation/{id}.html",
    "run": "run/{id}.html",
    "static": "static/{id}",
}


def url_builder(mode: str, rel: str = "") -> Callable[..., str | None]:
    table = APP_PATHS if mode == "app" else STATIC_PATHS

    def u(kind: str, id: Any = None) -> str | None:
        pattern = table[kind]
        if pattern is None:
            return None
        path = pattern.format(id=id) if id is not None else pattern
        return path if mode == "app" else rel + path

    return u


def fmt_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime | date):
        return value.strftime("%d %b %Y").lstrip("0")
    return str(value)


def fmt_datetime(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(ZoneInfo(get_settings().timezone))
        return value.strftime("%d %b %Y, %H:%M").lstrip("0")
    return fmt_date(value)


def deadline_iso(value: Any) -> str:
    """A portal deadline (naive Brussels time) as ISO 8601 with its offset, for the
    client-side countdown."""
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return ""
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day, 23, 59, 59)
    zone = ZoneInfo(get_settings().timezone)
    value = value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)
    return value.isoformat(timespec="seconds")


def fmt_num(value: Any) -> str:
    if value is None:
        return "0"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def fmt_ms(value: Any) -> str:
    if value is None:
        return ""
    ms = int(value)
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def pct(part: Any, whole: Any) -> str:
    try:
        whole = float(whole)
        return "0%" if whole == 0 else f"{100 * float(part) / whole:.0f}%"
    except (TypeError, ValueError):
        return ""


SHORT_CATEGORIES = {
    "Non-governmental organisations, platforms and networks and similar": "NGO",
    "Companies & groups": "Company",
    "Trade and business associations": "Business association",
    "Trade unions and professional associations": "Trade union or professional body",
    "Professional consultancies": "Consultancy",
    "Think tanks and research institutions": "Think tank",
    "Other organisations, public or mixed entities": "Public or mixed body",
    "Academic institutions": "University",
    "Associations and networks of public authorities": "Network of public authorities",
    "Self-employed individuals": "Self-employed",
    "Law firms": "Law firm",
    "Organisations representing churches and religious communities": "Religious organisation",
    "Entities, offices or networks established by third countries": "Third-country office",
}


PLACEHOLDER_NAMES = frozenset({"", "none", "n/a", "na", "-", ".", "x", "null"})


def submitter_name(name: str | None) -> str:
    """A submitter that typed "None" or "-" as its organisation reads as a bug; say so plainly."""
    return "No name given" if (name or "").strip().lower() in PLACEHOLDER_NAMES else name


def short_category(category: str | None) -> str:
    return SHORT_CATEGORIES.get(category or "", category or "")


def build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        {
            "date": fmt_date,
            "datetime": fmt_datetime,
            "num": fmt_num,
            "ms": fmt_ms,
            "pct": pct,
            "deadline_iso": deadline_iso,
            "short_category": short_category,
            "submitter_name": submitter_name,
        }
    )
    env.globals["Markup"] = Markup
    return env


class Renderer:
    def __init__(self, mode: str = "app", snapshot_date: str | None = None) -> None:
        self.mode = mode
        self.env = build_env()
        # shown in the footer of every page; the export sets it once, the app per request
        self.snapshot_date = snapshot_date

    def render(self, template: str, *, rel: str = "", **context: Any) -> str:
        context.setdefault("snapshot_date", self.snapshot_date)
        # house rule: no long dash on a page, register names and titles included
        return plain_dashes(
            self.env.get_template(template).render(
                u=url_builder(self.mode, rel),
                static_mode=self.mode == "static",
                now=portal_now(),
                **context,
            )
        )
