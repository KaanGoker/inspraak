"""Settings read from the environment (and .env). One place, no globals elsewhere."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    data_dir: Path
    embed_model: str
    embed_dim: int
    user_agent: str
    hys_base: str
    register_url: str
    meetings_url: str
    request_interval: float
    scope_open_days: int
    scope_closed_max: int
    scope_closed_min_org: int
    scope_closed_lookback_days: int
    scope_closed_scan_limit: int
    feedback_max_rows: int
    keep_attachment_files: bool
    cache_hours: float
    timezone: str
    # agent layer
    llm_provider: str
    gemini_api_key: str
    gemini_model: str
    llm_rpm: int
    llm_rpd: int
    llm_max_attempts: int
    llm_max_output_tokens: int
    run_inline: bool
    match_top_k: int
    match_min_score: float
    match_uncertain_score: float
    match_rerank: bool
    deadline_days: int
    draft_max_chars: int
    analyst_min_confidence: float
    review_token: str
    write_rate_per_10min: int

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://inspraak:inspraak@localhost:5544/inspraak"
        ),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6390/0"),
        data_dir=Path(os.environ.get("DATA_DIR", PROJECT_ROOT / "data")),
        embed_model=os.environ.get(
            "EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        ),
        embed_dim=_int("EMBED_DIM", 384),
        user_agent=os.environ.get("HTTP_USER_AGENT", "inspraak/0.1 (research snapshot, 1 req/s)"),
        hys_base=os.environ.get("HYS_BASE", "https://ec.europa.eu/info/law/better-regulation"),
        register_url=os.environ.get(
            "REGISTER_URL", "https://transparency-register.europa.eu/odplastorganisationxml_en"
        ),
        meetings_url=os.environ.get(
            "MEETINGS_URL",
            "https://ec.europa.eu/transparency-initiative/meetings/data/meetings/dataxlsx"
            "?name=meetingscommissionrepresentatives2429",
        ),
        request_interval=_float("REQUEST_INTERVAL", 1.0),
        scope_open_days=_int("SCOPE_OPEN_DAYS", 90),
        scope_closed_max=_int("SCOPE_CLOSED_MAX", 40),
        scope_closed_min_org=_int("SCOPE_CLOSED_MIN_ORG", 20),
        scope_closed_lookback_days=_int("SCOPE_CLOSED_LOOKBACK_DAYS", 365),
        scope_closed_scan_limit=_int("SCOPE_CLOSED_SCAN_LIMIT", 250),
        feedback_max_rows=_int("FEEDBACK_MAX_ROWS", 5000),
        keep_attachment_files=os.environ.get("KEEP_ATTACHMENT_FILES", "0") == "1",
        cache_hours=_float("CACHE_HOURS", 20.0),
        timezone=os.environ.get("TZ_NAME", "Europe/Brussels"),
        llm_provider=os.environ.get("LLM_PROVIDER", "fake"),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        # Verified 3 October 2026 with a real call: served on the free tier, structured
        # output works. The pricing page lists it as free of charge.
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        # Self-imposed budget, shared through Redis by every process. Google no longer
        # publishes a free-tier table (the docs point to AI Studio); third-party pages
        # report 30 requests per minute and 1,000 per day for flash-lite. Stay well under.
        llm_rpm=_int("LLM_RPM", 10),
        llm_rpd=_int("LLM_RPD", 300),
        llm_max_attempts=_int("LLM_MAX_ATTEMPTS", 4),
        llm_max_output_tokens=_int("LLM_MAX_OUTPUT_TOKENS", 4096),
        run_inline=os.environ.get("RUN_INLINE", "0") == "1",
        match_top_k=_int("MATCH_TOP_K", 5),
        match_min_score=_float("MATCH_MIN_SCORE", 0.45),
        match_uncertain_score=_float("MATCH_UNCERTAIN_SCORE", 0.55),
        match_rerank=os.environ.get("MATCH_RERANK", "1") != "0",
        deadline_days=_int("DEADLINE_DAYS", 7),
        draft_max_chars=_int("DRAFT_MAX_CHARS", 4000),
        analyst_min_confidence=_float("ANALYST_MIN_CONFIDENCE", 0.6),
        # When set, every page but the static files and the health check asks for HTTP
        # basic auth with this password (any user name). Compose binds the ports to
        # 127.0.0.1 either way.
        review_token=os.environ.get("REVIEW_TOKEN", ""),
        write_rate_per_10min=_int("WRITE_RATE_PER_10MIN", 20),
    )


# Have Your Say end dates are stored as naive wall-clock times in Brussels (deadlines end
# at 23:59:59 Brussels time). Every "is it still open" check compares against this clock,
# never the machine's local clock or the database's UTC now().
PORTAL_TZ = "Europe/Brussels"
PORTAL_NOW_SQL = f"(now() AT TIME ZONE '{PORTAL_TZ}')"


def portal_now() -> datetime:
    """Naive current time in Brussels, comparable with the stored end dates."""
    return datetime.now(ZoneInfo(PORTAL_TZ)).replace(tzinfo=None)
