"""24.35, the MCP half: a tool's unexpected error reaches the model only as an error id;
the full error is in the server's log under that id. Errors a tool raises on purpose keep
their message, which the model needs to correct its call."""

from __future__ import annotations

import logging
import re

import pytest

from dip.agents.approvals import ApprovalError
from dip.agents.mcp_server import tool_error
from dip.contracts import ContractError

SECRET_DETAIL = 'connection to server at "postgres.dip-data" (10.96.0.12) failed: FATAL'


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError(SECRET_DETAIL),
        OSError(SECRET_DETAIL),
        KeyError(SECRET_DETAIL),
        IndexError(SECRET_DETAIL),
        TypeError(SECRET_DETAIL),
    ],
)
def test_an_unexpected_error_is_an_id_for_the_model_and_logged_in_full(exc, caplog):
    with caplog.at_level(logging.ERROR, logger="dip.mcp"):
        result = tool_error("get_feed_history", exc)
    text = result.content[0].text
    assert result.is_error
    m = re.fullmatch(r"internal error \(error ([0-9a-f]{12})\)", text)
    assert m, text
    assert SECRET_DETAIL not in text and type(exc).__name__ not in text
    [record] = caplog.records
    assert m[1] in record.getMessage() and "get_feed_history" in record.getMessage()
    assert record.exc_info and record.exc_info[1] is exc


def test_each_error_gets_its_own_id():
    a = tool_error("t", RuntimeError("x")).content[0].text
    b = tool_error("t", RuntimeError("x")).content[0].text
    assert a != b


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("feed must be a contract feed name"),
        LookupError("Kubernetes is not configured for this server"),
        ApprovalError("approval 3 is not approved"),
        ContractError("column member_id: unknown type 'strng'"),
    ],
)
def test_an_error_raised_for_the_model_keeps_its_message(exc, caplog):
    with caplog.at_level(logging.ERROR, logger="dip.mcp"):
        result = tool_error("t", exc)
    assert result.is_error
    assert result.content[0].text == f"{type(exc).__name__}: {exc}"
    assert not caplog.records


def test_an_approval_that_fails_unexpectedly_keeps_only_an_id(caplog):
    """approvals.execute (an agent can run it through execute_approved_operation) uses the
    same rule: its stored and raised error is an id unless the error is for the model."""
    import inspect

    from dip.agents import approvals
    from dip.agents.errors import for_the_model, hidden

    src = inspect.getsource(approvals.execute)
    assert "for_the_model(exc)" in src and "hidden(log" in src
    assert for_the_model(ApprovalError("x")) and not for_the_model(RuntimeError("x"))
    with caplog.at_level(logging.ERROR, logger="dip.approvals"):
        text = hidden(approvals.log, "approval 7", RuntimeError(SECRET_DETAIL))
    assert re.fullmatch(r"internal error \(error [0-9a-f]{12}\)", text)
    assert SECRET_DETAIL not in text and caplog.records[0].exc_info
