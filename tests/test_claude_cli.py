from __future__ import annotations

import json
import subprocess

import pytest

from inspraak.llm.base import LLMError
from inspraak.llm.bucket import QuotaExhausted
from inspraak.llm.claude_cli import ClaudeCliProvider
from inspraak.llm.schemas import FeedbackAnalysis

ANSWER = {
    "stance": "wants_changes",
    "confidence": 0.9,
    "key_asks": [{"ask": "Protect small firms", "quote": "Article 5 must protect small firms"}],
}


def _fake_run(payloads: list[dict], seen: list[list[str]]):
    def run(cmd, input, capture_output, text, timeout):
        seen.append(cmd)
        out = payloads.pop(0)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")

    return run


def test_structured_output_is_parsed_and_flags_are_set(monkeypatch):
    seen: list[list[str]] = []
    ok = {"is_error": False, "structured_output": ANSWER, "usage": {"input_tokens": 1200}}
    monkeypatch.setattr(subprocess, "run", _fake_run([ok], seen))
    provider = ClaudeCliProvider("haiku", "low", sleep=lambda s: None)
    result = provider.complete_json("text", FeedbackAnalysis, purpose="feedback_analysis")
    assert result.data.stance == "wants_changes"
    assert result.provider == "claude_cli"
    assert result.prompt_tokens == 1200
    cmd = seen[0]
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--json-schema" in cmd and "--no-session-persistence" in cmd


def test_cli_error_is_retried_then_succeeds(monkeypatch):
    seen: list[list[str]] = []
    bad = {"is_error": True, "result": "API Error: 529 overloaded"}
    ok = {"is_error": False, "structured_output": ANSWER, "usage": {}}
    monkeypatch.setattr(subprocess, "run", _fake_run([bad, ok], seen))
    provider = ClaudeCliProvider("haiku", sleep=lambda s: None)
    result = provider.complete_json("text", FeedbackAnalysis, purpose="feedback_analysis")
    assert result.attempts == 2 and len(seen) == 2


def test_usage_limit_stops_the_job(monkeypatch):
    bad = {"is_error": True, "result": "Claude AI usage limit reached"}
    monkeypatch.setattr(subprocess, "run", _fake_run([bad], []))
    provider = ClaudeCliProvider("haiku", sleep=lambda s: None)
    with pytest.raises(QuotaExhausted):
        provider.complete_json("text", FeedbackAnalysis, purpose="feedback_analysis")


def test_garbage_stdout_fails_after_attempts(monkeypatch):
    def run(cmd, input, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="not json", stderr="boom")

    monkeypatch.setattr(subprocess, "run", run)
    provider = ClaudeCliProvider("haiku", max_attempts=2, sleep=lambda s: None)
    with pytest.raises(LLMError):
        provider.complete_json("text", FeedbackAnalysis, purpose="feedback_analysis")


def test_batch_answer_must_echo_its_own_opening_words():
    from inspraak.jobs.batch import opening_matches

    text = "BusinessEurope welcomes the proposal but asks for a longer transition period."
    assert opening_matches("BusinessEurope welcomes the proposal but asks for a", text)
    assert opening_matches("businesseurope  welcomes the proposal but", text)
    assert not opening_matches("Greenpeace opposes the proposal because it weakens", text)
    assert not opening_matches("BusinessEurope welcomes", text)  # too short to tie
