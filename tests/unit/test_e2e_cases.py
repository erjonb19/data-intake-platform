"""The E2E case table (tests/e2e/cases.yaml) checked offline, so a broken case fails in
`pytest`, not only on the kind cluster."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime

import pytest

from dip.contracts import REPO_ROOT, dag_id_for, load_contracts
from dip.csvfeed import validate_csv
from dip.excelfeed import validate_excel


def _runner():
    spec = importlib.util.spec_from_file_location("e2e_run", REPO_ROOT / "tests/e2e/run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = _runner()
CASES = [
    (feed, spec, case) for feed, spec in RUN.load_cases()["feeds"].items() for case in spec["cases"]
]


def test_runner_names_dags_like_the_dag_factory():
    for feed in load_contracts():
        assert RUN.dag_id_for(feed) == dag_id_for(feed)


@pytest.mark.parametrize(("feed", "spec", "case"), CASES, ids=[c["fixture"] for _, _, c in CASES])
def test_case_matches_what_the_contract_does(feed, spec, case):
    contract = load_contracts()[feed]
    data = (RUN.FIXTURES / case["fixture"]).read_bytes()
    validate = validate_excel if contract.format_type == "excel" else validate_csv
    result = validate(data, contract, datetime(2026, 10, 21, tzinfo=UTC))
    reasons = sorted({v.reason_code for v in result.file_violations} |
                     {v.reason_code for _, v in result.rejected})  # fmt: skip
    assert reasons == sorted(case["reasons"])
    assert sorted(r.row_number for r, _ in result.rejected) == sorted(case.get("exclude_rows", []))
    assert len(result.accepted) == case["gold_rows"]
    # The checksum columns are the contract's columns, and the checksum can be computed.
    assert spec["checksum_columns"] == [c["name"] for c in contract.spec["columns"]]
    if case["gold_rows"]:
        n, _ = RUN.fixture_checksum(
            data, set(case.get("exclude_rows", [])), spec["checksum_columns"], spec.get("excel")
        )
        assert n == case["gold_rows"]
    if contract.format_type == "excel":
        # The runner reads workbooks by the same rules as the intake's reader.
        fmt = contract.spec["format"]
        assert spec["excel"] == {"sheet": fmt["sheet"], "header_row": fmt["header_row"]}
        if not result.file_violations:
            expected = [r.record for r in result.accepted]
            got = RUN._xlsx_rows(data, fmt["sheet"], fmt["header_row"])
            kept = [r for i, r in enumerate(got, 1) if i not in set(case.get("exclude_rows", []))]
            assert kept == expected


@pytest.mark.parametrize(("feed", "spec", "case"), CASES, ids=[c["fixture"] for _, _, c in CASES])
def test_case_channel_is_one_its_contract_allows(feed, spec, case):
    channel = case.get("channel", "minio")
    assert channel in ("minio", "sftp", "api")
    assert channel in load_contracts()[feed].spec["source"]["channels"]
