"""The free fallback, per worker step (Stage 10, S10.1; dip.agents.llm.Failover): which
errors move a step to the next provider, and that a step which already wrote never does."""

from __future__ import annotations

import asyncio

import pytest

from dip.agents import chain_job
from dip.agents.gemini import GeminiError
from dip.agents.llm import Failover, NoProvider, StepWrote, is_failover, provider_names


class Status(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class Fake:
    def __init__(self, name: str, error: Exception | None = None):
        self.name, self.error = name, error


def run(providers, wrote=lambda: False, seen=None):
    seen = [] if seen is None else seen

    async def step(llm):
        seen.append(llm.name)
        if llm.error:
            raise llm.error
        return f"answered by {llm.name}"

    return asyncio.run(Failover(providers).run(step, wrote)), seen


@pytest.mark.parametrize("status", [429, 529, 500, 503])
def test_an_unavailable_claude_finishes_the_step_on_gemini(status):
    (out, provider), seen = run([("anthropic", lambda: Fake("claude", Status(status))),
                                 ("gemini", lambda: Fake("gemini"))])  # fmt: skip
    assert (out, provider) == ("answered by gemini", "gemini")
    assert seen == ["claude", "gemini"]  # the whole step again, from its task


def test_a_missing_key_skips_that_provider():
    def no_key():
        raise LookupError("ANTHROPIC_API_KEY is not set")

    (_, provider), seen = run([("anthropic", no_key), ("gemini", lambda: Fake("gemini"))])
    assert provider == "gemini" and seen == ["gemini"]


def test_our_own_errors_are_not_retried_elsewhere():
    with pytest.raises(Status):  # a 400 is a malformed request: Gemini would get it too
        run([("anthropic", lambda: Fake("claude", Status(400))),
             ("gemini", lambda: Fake("gemini"))])  # fmt: skip
    with pytest.raises(KeyError):
        run([("anthropic", lambda: Fake("claude", KeyError("bug"))),
             ("gemini", lambda: Fake("gemini"))])  # fmt: skip


def test_a_step_that_already_wrote_is_not_restarted():
    seen: list[str] = []
    with pytest.raises(StepWrote, match="after the step had written"):
        run([("anthropic", lambda: Fake("claude", Status(529))),
             ("gemini", lambda: Fake("gemini"))], wrote=lambda: True, seen=seen)  # fmt: skip
    assert seen == ["claude"]


def test_when_every_provider_fails_the_step_says_why():
    with pytest.raises(NoProvider, match="anthropic: Status: HTTP 529; gemini: GeminiError"):
        run([("anthropic", lambda: Fake("claude", Status(529))),
             ("gemini", lambda: Fake("gemini", GeminiError("Gemini 503: busy", 503)))])  # fmt: skip


def test_which_errors_mean_unavailable():
    assert is_failover(GeminiError("Gemini unreachable: timed out"))
    assert not is_failover(GeminiError("Gemini 400: bad request", 400))
    assert not is_failover(ValueError("x"))
    # The MCP client's task group wraps what failed inside a step.
    assert is_failover(ExceptionGroup("step", [Status(529)]))
    assert not is_failover(ExceptionGroup("step", [Status(529), KeyError("bug")]))


def test_providers_follow_the_config_order_and_need_a_key():
    assert provider_names({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"}) == [
        "anthropic", "gemini"]  # fmt: skip
    assert provider_names({"GEMINI_API_KEY": "g"}) == ["gemini"]
    assert provider_names({"ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": ""}) == []


def test_the_chain_job_tries_every_configured_provider():
    mcp = {"DIP_MCP_URL": "u", "DIP_INCIDENT_KEY": "k1", "DIP_TRIAGE_KEY": "k2"}
    p = chain_job.plan({**mcp, "ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"})
    assert p.runnable and p.providers == ("anthropic", "gemini")
