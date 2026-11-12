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
    monkeypatch.delenv("DIP_MONTHLY_CAP_USD", raising=False)


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


def test_a_long_request_is_priced_at_the_long_context_rate(monkeypatch):
    monkeypatch.setenv("DIP_SPEND_CAP_USD", "0.05")
    m = model({"input_tokens": 10, "output_tokens": 10}, max_tokens=0)
    # ~150K tokens, with 1.25x for the cache write: $0.019 at the base rate (under the
    # cap), $0.094 at the long-context rate (over it)
    with pytest.raises(SpendCap):
        m.create(system="s" * 450_000, tools=[], messages=[])


def test_a_cache_write_costs_more_than_a_read():
    write = llm.cost_usd([{"cache_creation_input_tokens": 1_000_000}], "claude-haiku-5-5")
    read = llm.cost_usd([{"cache_read_input_tokens": 1_000_000}], "claude-haiku-5-5")
    assert write == pytest.approx(0.125) and read == pytest.approx(0.10)


# --- 24.3: the monthly cap, counted from ops.model_call, and unpriced models -------------


def test_the_monthly_cap_counts_this_months_live_spend(monkeypatch):
    monkeypatch.delenv("DIP_SPEND_CAP_USD", raising=False)
    monkeypatch.setenv("DIP_MONTHLY_CAP_USD", "5")
    m = model({"input_tokens": 10, "output_tokens": 10}, max_tokens=1000)
    token = llm.MONTH_METER.set(lambda: 4.0)
    try:
        call(m)  # $4 spent, the worst case is a fraction of a cent
        llm.MONTH_METER.set(lambda: 4.9999)
        with pytest.raises(SpendCap, match="5.00 monthly cap"):
            call(m)
    finally:
        llm.MONTH_METER.reset(token)
    assert m.client.calls == 1


def test_the_monthly_cap_fails_closed_without_a_meter(monkeypatch):
    monkeypatch.setenv("DIP_MONTHLY_CAP_USD", "5")
    m = model({"input_tokens": 10, "output_tokens": 10})
    with pytest.raises(SpendCap, match="cannot be counted"):
        call(m)
    assert m.client.calls == 0


def test_the_monthly_cap_fails_closed_when_the_meter_fails(monkeypatch):
    monkeypatch.setenv("DIP_MONTHLY_CAP_USD", "5")
    m = model({"input_tokens": 10, "output_tokens": 10})

    def broken():
        raise ConnectionError("password=secret in a driver message")

    token = llm.MONTH_METER.set(broken)
    try:
        with pytest.raises(SpendCap, match="ConnectionError") as e:
            call(m)
    finally:
        llm.MONTH_METER.reset(token)
    assert "secret" not in str(e.value)  # only the error's type, never its text
    assert m.client.calls == 0


def test_an_unpriced_model_is_refused(monkeypatch):
    monkeypatch.delenv("DIP_SPEND_CAP_USD", raising=False)
    m = model({"input_tokens": 10, "output_tokens": 10})
    m.model = "claude-opus-5-5"
    with pytest.raises(SpendCap, match="no price"):
        call(m)
    assert m.client.calls == 0
    assert not is_failover(SpendCap("no price"))


def test_spend_caps_missing_only_for_a_paid_provider():
    assert llm.spend_caps_missing(["gemini"], {}) is None
    assert "DIP_MONTHLY_CAP_USD" in llm.spend_caps_missing(
        ["anthropic"], {"DIP_SPEND_CAP_USD": "1"})  # fmt: skip
    assert llm.spend_caps_missing(
        ["anthropic"], {"DIP_SPEND_CAP_USD": "1", "DIP_MONTHLY_CAP_USD": "5"}) is None  # fmt: skip


def test_spend_cap_of_finds_a_cap_inside_an_exception_group():
    cap = SpendCap("over")
    assert llm.spend_cap_of(cap) is cap
    assert llm.spend_cap_of(ExceptionGroup("g", [ValueError(), ExceptionGroup("h", [cap])])) is cap
    assert llm.spend_cap_of(ExceptionGroup("g", [ValueError()])) is None
