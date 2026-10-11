"""Repository rules that are easy to break by accident."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from inspraak.textutil import plain_dashes

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(
    "[" + "".join(chr(c) for c in (0x2012, 0x2013, 0x2014, 0x2015, 0x2212)) + "]"
)
TEXT_SUFFIXES = {".py", ".md", ".sql", ".toml", ".yml", ".yaml", ".json", ".xml", ".html",
                 ".css", ".js", ".txt", ".example", ""}  # fmt: skip
SKIP_DIRS = {".git", ".venv", "data", "research", "site", "__pycache__", ".pytest_cache",
             ".ruff_cache"}  # fmt: skip


def _project_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in ("Makefile", ".gitignore"):
            files.append(path)
    return files


def test_no_long_dashes_or_unicode_minus_in_the_repository():
    offenders = []
    for path in _project_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if FORBIDDEN.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_plain_dashes():
    en, em, minus = chr(0x2013), chr(0x2014), chr(0x2212)
    assert plain_dashes(f"2024{en}2029 {em} a {minus} b") == "2024-2029 - a - b"
    assert plain_dashes("plain-text") == "plain-text"


def test_secrets_and_data_are_gitignored():
    for name in (".env", "data/raw/hys/listing/page-0000.json", ".venv/pyvenv.cfg"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", name], cwd=ROOT, capture_output=True, check=False
        )
        assert result.returncode == 0, f"{name} is not ignored"
