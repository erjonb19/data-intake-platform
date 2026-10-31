"""The spend cap (Stage 10, S10.6's proof workflow; dip.agents.llm.check_spend): with
DIP_SPEND_CAP_USD set, no request is sent that could take the process past it, and the
cap is not a failover error, so no other provider takes the step over."""

from __future__ import annotations

import pytest

from dip.agents import llm
from dip.agents.llm import AnthropicLLM, SpendCap, is_failover


class Response:
    def __init__(self, usage):
        self.usage = usage

    def to_dict(self):
        return {"content": [], "stop_reason": "end_turn", "usage": self.usage, "model": "m"}


class Client:
    def __init__(self, usage):
        self.calls = 0
        self.messages = self
        self.usage = usage

    def create(self, **kwargs):
        self.calls += 1
        return Response(self.usage)


def model(usage, max_tokens=1000):
    m = AnthropicLLM.__new__(AnthropicLLM)
    m.model, m.effort, m.max_tokens = "claude-haiku-5-5", "medium", max_tokens
    m.client = Client(usage)
    return m


@pytest.fixture(autouse=True)
def fresh_meter(monkeypatch):
    monkeypatch.setitem(llm.SPENT, "usd", 0.0)


def call(m):
    return m.create(system="s", tools=[], messages=[{"role": "user", "content": "x"}])


def test_without_a_cap_every_call_is_sent_and_metered(monkeypatch):
    monkeypatch.delenv("DIP_SPEND_CAP_USD", raising=False)
    m = model({"input_tokens": 1_000_000, "output_tokens": 0})  # $0.10 a call
    for _ in range(3):
        call(m)
    assert m.client.calls == 3
    assert llm.SPENT["usd"] == pytest.approx(0.30)


def test_the_call_that_could_pass_the_cap_is_never_sent(monkeypatch):
    monkeypatch.setenv("DIP_SPEND_CAP_USD", "0.25")
    m = model({"input_tokens": 1_000_000, "output_tokens": 0})
    call(m)
    call(m)  # $0.20 spent; the worst case of a third ($0.0005 of output) still fits
    with pytest.raises(SpendCap, match="0.25 cap"):
        m.max_tokens = 200_000  # a worst case of $0.10 more
        call(m)
    assert m.client.calls == 2
    assert llm.SPENT["usd"] == pytest.approx(0.20)


def test_the_cap_counts_the_request_size(monkeypatch):
    monkeypatch.setenv("DIP_SPEND_CAP_USD", "0.01")
    m = model({"input_tokens": 10, "output_tokens": 10})
    with pytest.raises(SpendCap):  # ~3 M characters: about $0.10 of input
        m.create(system="s" * 3_000_000, tools=[], messages=[])
    assert m.client.calls == 0


def test_the_cap_is_not_a_failover():
    assert not is_failover(SpendCap("over"))
