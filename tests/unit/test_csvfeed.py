"""CSV contracts (Stage 1): file-level schema drift, row checks and the bad-row ratio,
on the eligibility contract and hand-written, invented fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING, BUCKET_QUARANTINE
from dip.contracts import Contract, load_contracts
from dip.csvfeed import validate_csv
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.unit.test_intake import FakeWarehouse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eligibility"
FEED = "healthcare.eligibility"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
HEADER = "member_id,first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,patient_ref"
GOOD_ROW = "M000000001,Alex,Example,1980-02-14,F,HMO,2024-01-01,,ref-1"


@pytest.fixture(scope="module")
def contract() -> Contract:
    return load_contracts()[FEED]


def check(contract, *lines: str, **overrides):
    if overrides:
        contract = Contract(
            contract.feed, contract.version, {**contract.spec, **overrides}, contract.path
        )
    return validate_csv(("\n".join(lines) + "\n").encode("utf-8"), contract, NOW)


def codes(result):
    return sorted(v.reason_code for v in result.file_violations)


def test_good_fixture_loads_every_row(contract):
    r = validate_csv((FIXTURES / "good/elig_good.csv").read_bytes(), contract, NOW)
    assert not r.file_rejected and not r.rejected
    assert [row.row_number for row in r.accepted] == [1, 2, 3, 4, 5, 6]
    # Values are kept as received (text); empty stays empty.
    assert r.accepted[0].record["coverage_end"] == ""


def test_missing_column_quarantines_the_file(contract):
    r = validate_csv((FIXTURES / "bad/elig_missing_member_id.csv").read_bytes(), contract, NOW)
    assert codes(r) == ["SCHEMA.MISSING_COLUMN:member_id"]
    assert r.accepted == []


def test_unexpected_and_duplicate_columns(contract):
    assert codes(check(contract, HEADER + ",extra", GOOD_ROW + ",x")) == [
        "SCHEMA.UNEXPECTED_COLUMN:extra"
    ]
    assert (
        codes(check(contract, HEADER + ",extra", GOOD_ROW + ",x", allow_extra_columns=True)) == []
    )
    assert codes(check(contract, HEADER + ",sex", GOOD_ROW + ",F")) == [
        "SCHEMA.DUPLICATE_COLUMN:sex"
    ]


def test_column_order_does_not_matter(contract):
    cols = HEADER.split(",")
    vals = GOOD_ROW.split(",")
    r = check(contract, ",".join(reversed(cols)), ",".join(reversed(vals)))
    assert not r.file_rejected and len(r.accepted) == 1


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("member_id", "", "VALUE.REQUIRED:member_id"),
        ("member_id", "X123", "VALUE.PATTERN:member_id"),
        ("dob", "02/14/1980", "VALUE.TYPE:dob"),
        ("dob", "2026-10-22", "VALUE.OUT_OF_RANGE:dob"),  # after "today" (the received date)
        ("dob", "1899-12-31", "VALUE.OUT_OF_RANGE:dob"),
        ("sex", "X", "VALUE.NOT_ALLOWED:sex"),
        ("plan_code", "GOLD", "VALUE.NOT_ALLOWED:plan_code"),
        ("coverage_end", "soon", "VALUE.TYPE:coverage_end"),
    ],
)
def test_row_checks(contract, field, value, code):
    cols = HEADER.split(",")
    vals = GOOD_ROW.split(",")
    vals[cols.index(field)] = value
    # reject_file, so a single bad row is reported at file level too.
    r = check(contract, HEADER, ",".join(vals), on_row_violation="reject_file")
    assert [v.reason_code for _, v in r.rejected] == [code]
    assert codes(r) == ["FILE.ROW_VIOLATIONS"]


def test_today_is_the_received_date(contract):
    row = GOOD_ROW.replace("1980-02-14", "2026-10-21")
    assert not check(contract, HEADER, row).rejected


def test_duplicate_keys_reject_every_copy(contract):
    other = "M000000002,Sam,Sample,1975-07-30,M,PPO,2023-06-01,,"
    r = check(contract, HEADER, GOOD_ROW, other, GOOD_ROW, max_bad_row_ratio=1)
    assert [(row.row_number, v.reason_code) for row, v in r.rejected] == [
        (1, "KEY.DUPLICATE:member_id"),
        (3, "KEY.DUPLICATE:member_id"),
    ]
    assert [row.row_number for row in r.accepted] == [2]


def test_field_count_and_parse_errors(contract):
    r = check(contract, HEADER, GOOD_ROW + ",extra", max_bad_row_ratio=1)
    assert [v.reason_code for _, v in r.rejected] == ["ROW.FIELD_COUNT"]
    assert codes(check(contract, HEADER, 'M000000001,"unterminated')) == ["FILE.PARSE_ERROR"]


def test_encoding_empty_and_header_only(contract):
    assert codes(validate_csv(b"\xff\xfe\x00bad", contract, NOW)) == ["FILE.ENCODING"]
    assert codes(validate_csv(b"\n\n", contract, NOW)) == ["FILE.EMPTY"]
    assert codes(check(contract, HEADER)) == ["FILE.TOO_FEW_ROWS"]
    # A UTF-8 byte order mark is not part of the first column name.
    assert not validate_csv(
        ("\ufeff" + HEADER + "\n" + GOOD_ROW).encode(), contract, NOW
    ).file_rejected


def test_bad_row_ratio_boundary(contract):
    # 1 bad row in 20 is exactly the 5% limit: the good 19 load.
    data = (FIXTURES / "bad/elig_bad_rows.csv").read_bytes()
    r = validate_csv(data, contract, NOW)
    assert not r.file_rejected
    assert len(r.accepted) == 19
    assert [(row.row_number, v.reason_code) for row, v in r.rejected] == [
        (7, "VALUE.NOT_ALLOWED:plan_code")
    ]
    # One more bad row is above it: the whole file is rejected and nothing loads.
    lines = data.decode().splitlines()
    lines[3] = lines[3].replace(",EPO,", ",GOLD,").replace(",HDHP,", ",GOLD,")
    r = check(contract, *lines)
    assert codes(r) == ["FILE.BAD_ROW_RATIO"]
    assert r.accepted == []


def test_intake_loads_good_rows_and_quarantines_bad_ones(tmp_path):
    store = LocalStore(tmp_path)
    wh = FakeWarehouse()
    for name in ("good/elig_good.csv", "bad/elig_missing_member_id.csv", "bad/elig_bad_rows.csv"):
        store.put(BUCKET_LANDING, f"{FEED}/{Path(name).name}", (FIXTURES / name).read_bytes())
    # A file whose name does not match the contract's pattern is quarantined too (other
    # bytes than the good file, or idempotency would skip it as a duplicate).
    misnamed = (FIXTURES / "good/elig_good.csv").read_bytes() + b"\n"
    store.put(BUCKET_LANDING, f"{FEED}/members.csv", misnamed)
    contracts = load_contracts()
    out = {o.source_key.split("/")[-1]: o for o in process_landing(store, wh, contracts, NOW)}

    assert out["elig_good.csv"].status == "loaded" and out["elig_good.csv"].accepted == 6
    assert out["elig_missing_member_id.csv"].status == "quarantined"
    assert out["elig_missing_member_id.csv"].reason_codes == ("SCHEMA.MISSING_COLUMN:member_id",)
    bad_rows = out["elig_bad_rows.csv"]
    assert (bad_rows.status, bad_rows.accepted, bad_rows.rejected) == (
        "loaded_with_rejections",
        19,
        1,
    )
    assert out["members.csv"].reason_codes == ("FILE.NAME_PATTERN",)
    # Only accepted rows reach bronze: 6 + 19.
    assert len(wh.csv_rows) == 25
    # The rejected row is kept in quarantine with its reason, as received.
    [qkey] = bad_rows.quarantine_keys
    assert qkey.endswith(".rejected.ndjson")
    import json

    [doc] = [json.loads(line) for line in store.get(BUCKET_QUARANTINE, qkey).splitlines()]
    assert doc["reason_code"] == "VALUE.NOT_ALLOWED:plan_code"
    assert doc["row_number"] == 7 and doc["record"]["plan_code"] == "GOLD"


def _generator():
    import importlib.util

    from dip.contracts import REPO_ROOT

    path = REPO_ROOT / "packs/healthcare/generators/eligibility.py"
    spec = importlib.util.spec_from_file_location("eligibility_generator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_is_deterministic_and_valid(tmp_path, contract):
    import json
    import os

    from tests.fixtures.fhir.make_fixtures import good_bundle

    gen = _generator()
    (tmp_path / "p.json").write_text(json.dumps(good_bundle()), encoding="utf-8")
    first = gen.eligibility_csv(tmp_path)
    assert first == gen.eligibility_csv(tmp_path)
    r = validate_csv(first, contract, NOW)
    assert not r.file_rejected and not r.rejected and len(r.accepted) == 1
    # On the CI Synthea set too, when it is there: every generated row passes the contract.
    if synthea := os.environ.get("DIP_SYNTHEA_DIR"):
        r = validate_csv(gen.eligibility_csv(Path(synthea)), contract, NOW)
        assert not r.file_rejected and not r.rejected and r.accepted


def test_generator_leaves_out_members_who_died_before_the_plan(tmp_path):
    gen = _generator()
    row = gen.member_row(
        {"id": "x", "birthDate": "1930-01-01", "deceasedDateTime": "2015-05-05T00:00:00Z"}
    )
    assert row["coverage_end"] < row["coverage_start"]
    import json

    bundle = {"entry": [{"resource": {"resourceType": "Patient", "id": "x",
              "birthDate": "1930-01-01", "deceasedDateTime": "2015-05-05T00:00:00Z"}}]}  # fmt: skip
    (tmp_path / "dead.json").write_text(json.dumps(bundle), encoding="utf-8")
    assert gen.eligibility_csv(tmp_path).decode().count("\n") == 1  # header only
