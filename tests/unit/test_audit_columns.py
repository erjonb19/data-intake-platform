"""Stage 9 (S9.6): what an audit row is about, derived from the call."""

from dip.agents.registry import approval_of, chain_of


def test_chain_of_takes_the_chain_from_a_chain_steps_run_id():
    assert chain_of("chain-ab12/incident") == "chain-ab12"
    assert chain_of("eval-late_file/triage") == "eval-late_file"
    assert chain_of("chain-ab12/handoff") == "chain-ab12"
    for run_id in (None, "", "eval-mixed_day", "act-x/other", "/triage", "a/b/c"):
        assert chain_of(run_id) is None, run_id


def test_approval_of_reads_args_then_the_result():
    assert approval_of({"approval_id": 7}, {"approval_id": 9}) == 7
    assert approval_of({"group_id": "g"}, {"approval_id": 9, "status": "pending"}) == 9
    assert approval_of({}, {"error": "already pending", "pending_approval_id": 4}) == 4
    assert approval_of({"approval_id": "12"}, None) == 12
    assert approval_of({}, ["not", "a", "dict"]) is None
    assert approval_of({"approval_id": "x"}, None) is None
    assert approval_of({"approval_id": True}, {"approval_id": 3}) == 3
    assert approval_of({"approval_id": 12.0}, None) == 12
    assert approval_of({"approval_id": 12.5}, None) is None
