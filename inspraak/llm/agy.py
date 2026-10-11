"""Models through the Antigravity CLI (`agy --print`), for batch jobs on a machine where
it is logged in. agy has no schema flag: the JSON schema goes into the prompt and the
base class validates the answer (with its one corrective retry). A model chain: when
one model reports its quota spent, the next one takes over; QuotaExhausted only when
the whole chain is spent. Runs with an empty workspace, read-only plan mode."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
from typing import Any

from pydantic import BaseModel

from inspraak.llm.base import Provider, RawCompletion
from inspraak.llm.bucket import QuotaExhausted

TIMEOUT_SECONDS = 300
_QUOTA = re.compile(r"quota|exhaust|rate.?limit|resource_exhausted|429|limit reached", re.I)


class AgyError(RuntimeError):
    pass


class AgyQuota(AgyError):
    pass


def extract_json(text: str) -> str:
    """The first balanced JSON object in the output (agy may add prose or fences)."""
    start = text.find("{")
    if start < 0:
        raise AgyError(f"no JSON in output: {text[:200]!r}")
    depth = 0
    in_str = esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AgyError("unbalanced JSON in output")


class AgyProvider(Provider):
    name = "agy"

    def __init__(self, models: list[str], binary: str = "agy", **kwargs) -> None:
        super().__init__(models[0], **kwargs)
        self.models = list(models)
        self.binary = binary
        self.workspace = tempfile.mkdtemp(prefix="inspraak-agy-")
        self._lock = threading.Lock()

    def _generate(
        self,
        prompt: str,
        schema: type[BaseModel],
        *,
        system: str | None,
        temperature: float,
        max_output_tokens: int,
        context: dict[str, Any] | None,
    ) -> RawCompletion:
        full = (
            f"{system or ''}\n\n{prompt}\n\n"
            "Do not use any tools and do not read files. Answer with one JSON object only, "
            "no prose, that validates against this JSON schema:\n"
            f"{json.dumps(schema.model_json_schema())}"
        )
        while True:
            with self._lock:
                model = self.model
            try:
                return self._call(model, full)
            except AgyQuota:
                with self._lock:
                    if self.model == model:  # another thread may have moved on already
                        idx = self.models.index(model)
                        if idx + 1 >= len(self.models):
                            raise QuotaExhausted(
                                f"agy: every model is spent ({model} last)"
                            ) from None
                        self.model = self.models[idx + 1]

    def _call(self, model: str, prompt: str) -> RawCompletion:
        cmd = [
            self.binary,
            "--add-dir",
            self.workspace,
            "--model",
            model,
            "--mode",
            "plan",
            "--print",
            prompt,
        ]
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            err = (proc.stderr or out)[:400]
            if _QUOTA.search(err):
                raise AgyQuota(err)
            raise AgyError(f"exit {proc.returncode}: {err}")
        try:
            return RawCompletion(text=extract_json(out))
        except AgyError:
            if _QUOTA.search(out) and len(out) < 600:
                raise AgyQuota(out[:400]) from None
            raise

    def _is_retryable(self, exc: Exception) -> bool:
        return isinstance(exc, (AgyError, subprocess.TimeoutExpired)) and not isinstance(
            exc, AgyQuota
        )
