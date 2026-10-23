"""The E2E case table (tests/e2e/cases.yaml) checked offline, so a broken case fails in
`pytest`, not only on the kind cluster."""

from __future__ import annotations

import importlib.util
import shutil
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
    if contract.format_type == "pdf":
        return check_pdf_case(contract, spec, case, data)
    if contract.format_type == "x12":
        return check_x12_case(contract, spec, case, data)
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


# The gold.invoice_match columns a PDF case's `expect` gives, and the contract column each
# comes from (match_status is gold's own: the live E2E checks it).
PDF_COLUMNS = {
    "invoice_number": "invoice_number",
    "vendor_id": "vendor_id",
    "po_number": "po_number",
    "pdf_total": "total",
    "extraction": "extraction",
}


def check_pdf_case(contract, spec, case, data):
    if "scan" in case["fixture"] and shutil.which("tesseract") is None:
        pytest.skip("Tesseract is only in the OCR image and on CI")
    from dip.pdffeed import validate_pdf

    result = validate_pdf(data, contract, datetime(2026, 10, 21, tzinfo=UTC))
    reasons = sorted({v.reason_code for v in result.file_violations} |
                     {v.reason_code for _, v in result.rejected})  # fmt: skip
    assert reasons == sorted(case["reasons"])
    assert len(result.accepted) == case["gold_rows"] == len(case.get("expect", []))
    assert spec["checksum_columns"] == [*PDF_COLUMNS, "match_status"]
    for row, want in zip(result.accepted, case.get("expect", []), strict=True):
        assert {k: row.record[c] for k, c in PDF_COLUMNS.items()} == {
            k: str(want[k]) for k in PDF_COLUMNS
        }
        assert want["match_status"] in ("matched", "not_in_register", "vendor_differs",
                                        "po_differs", "total_differs")  # fmt: skip


def check_x12_case(contract, spec, case, data):
    from dip.x12feed import validate_x12

    result = validate_x12(data, contract, datetime(2026, 10, 21, tzinfo=UTC))
    reasons = sorted({v.reason_code for v in result.file_violations} |
                     {v.reason_code for _, v in result.rejected})  # fmt: skip
    assert reasons == sorted(case["reasons"])
    assert len(result.accepted) == case["gold_rows"] == len(case.get("expect", []))
    # The checksum columns are contract columns, and `expect` is exactly what was parsed.
    names = [c["name"] for c in contract.spec["columns"]]
    assert set(spec["checksum_columns"]) <= set(names)
    got = [{c: r.record[c] for c in spec["checksum_columns"]} for r in result.accepted]
    assert got == [{k: str(v) for k, v in want.items()} for want in case.get("expect", [])]


@pytest.mark.parametrize(("feed", "spec", "case"), CASES, ids=[c["fixture"] for _, _, c in CASES])
def test_case_channel_is_one_its_contract_allows(feed, spec, case):
    channel = case.get("channel", "minio")
    assert channel in ("minio", "sftp", "api")
    assert channel in load_contracts()[feed].spec["source"]["channels"]
