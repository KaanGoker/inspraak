"""On-disk raw cache under data/raw/hys. JSON for API pages, bytes for documents.

Every live response is written here before anything is parsed, and every fetch looks
here first, so an interrupted ingest resumes without repeating requests. Feedback
pages are scrubbed by the privacy guard before they are written.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from inspraak.config import get_settings


class RawCache:
    def __init__(self, root: Path | None = None, max_age_hours: float | None = None) -> None:
        settings = get_settings()
        self.root = root or settings.raw_dir / "hys"
        self.max_age = (settings.cache_hours if max_age_hours is None else max_age_hours) * 3600
        self.hits = 0
        self.misses = 0

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def _fresh(self, path: Path, forever: bool) -> bool:
        if not path.exists():
            return False
        if forever:
            return True
        return (time.time() - path.stat().st_mtime) <= self.max_age

    def get_json(self, *parts: str, forever: bool = False) -> Any | None:
        path = self.path(*parts)
        if not self._fresh(path, forever):
            self.misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.misses += 1
            return None
        self.hits += 1
        return payload

    def put_json(self, payload: Any, *parts: str) -> Path:
        path = self.path(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return path

    def get_bytes(self, *parts: str) -> bytes | None:
        path = self.path(*parts)
        if not path.exists():
            self.misses += 1
            return None
        self.hits += 1
        return path.read_bytes()

    def put_bytes(self, data: bytes, *parts: str) -> Path:
        path = self.path(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path
