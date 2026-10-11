"""Claude through the local Claude Code CLI (`claude -p`), for batch jobs on a machine
where the CLI is logged in. Structured output through `--json-schema`; no tools, no
settings, no session files. Model and effort come from CLAUDE_CLI_MODEL and
CLAUDE_CLI_EFFORT (default haiku, low)."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from pydantic import BaseModel

from inspraak.llm.base import Provider, RawCompletion

TIMEOUT_SECONDS = 180


class ClaudeCliError(RuntimeError):
    pass


class ClaudeCliProvider(Provider):
    name = "claude_cli"

    def __init__(self, model: str, effort: str = "low", binary: str = "claude", **kwargs) -> None:
        super().__init__(model, **kwargs)
        self.effort = effort
        self.binary = binary

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
        cmd = [
            self.binary,
            "-p",
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--json-schema",
            json.dumps(schema.model_json_schema()),
            "--system-prompt",
            system or "Answer with the structured output only.",
        ]
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_SECONDS
        )
        try:
            out = json.loads(proc.stdout)
        except ValueError as exc:
            raise ClaudeCliError(f"exit {proc.returncode}: {proc.stderr[:300]}") from exc
        if out.get("is_error") or out.get("structured_output") is None:
            raise ClaudeCliError(str(out.get("result"))[:300])
        usage = out.get("usage") or {}
        return RawCompletion(
            text=json.dumps(out["structured_output"]),
            prompt_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )

    def _is_rate_limit(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return "rate limit" in text or "usage limit" in text or "overloaded" in text

    def _is_daily_quota(self, exc: Exception) -> bool:
        return "usage limit" in str(exc).lower()

    def _is_retryable(self, exc: Exception) -> bool:
        return isinstance(exc, (ClaudeCliError, subprocess.TimeoutExpired))
