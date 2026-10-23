"""The X12 reader (Stage 4, S4.2) and the 837P/835 generator (S4.1): every envelope check,
the rows each transaction gives, and lines that balance. All X12 here is generated from
the invented claims fixture."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from dip.breakages import apply
from dip.contracts import REPO_ROOT, ContractError, load_contracts, parse_contract
from dip.x12feed import COLUMNS_835, COLUMNS_837, rows, validate_x12

spec = importlib.util.spec_from_file_location(
    "x12_gen", REPO_ROOT / "packs/healthcare/generators/x12.py"
)
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

CLAIMS = gen.read_claims(REPO_ROOT / "tests/fixtures/claims/good/claims_good.csv")
AT = datetime(2026, 10, 22, tzinfo=UTC)
C837 = load_contracts()["healthcare.claims_837"]
C835 = load_contracts()["healthcare.remittance_835"]


def codes(result):
    return sorted(
        {v.reason_code for v in result.file_violations}
        | {v.reason_code for _, v in result.rejected}
    )


def test_the_generator_is_deterministic_and_its_isa_is_fixed_width():
    a, b = gen.x12_837(CLAIMS, "20261021"), gen.x12_837(CLAIMS, "20261021")
    assert a == b
    assert len(a.decode().split("\n")[0]) == 106  # the ISA segment and its terminator
    assert a.decode()[105] == "~" and a.decode()[104] == ":" and a.decode()[3] == "*"


def test_837_lines_are_the_claims_file_lines():
    table = rows(gen.x12_837(CLAIMS, "20261021"), "837")
    assert table[0] == list(COLUMNS_837)
    got = sorted((r[1], r[11], r[13]) for r in table[1:])
    want = sorted((c["claim_line_id"], c["billed_amount"], c["service_date"]) for c in CLAIMS)
    assert got == want
    # A claim's total is the sum of its lines (CLM02), on both lines of the two-line claim.
    two = [dict(zip(COLUMNS_837, r, strict=True)) for r in table[1:] if r[0].endswith("0001")]
    assert {r["claim_total"] for r in two} == {"245.50"}
    assert {r["member_id"] for r in two} == {"M000000001"}


def test_835_lines_balance_and_match_the_claims_paid():
    table = rows(gen.x12_835(CLAIMS, "20261021"), "835")
    assert table[0] == list(COLUMNS_835)
    by_line = {c["claim_line_id"]: c for c in CLAIMS}
    for r in (dict(zip(COLUMNS_835, r, strict=True)) for r in table[1:]):
        src = by_line[r["claim_line_id"]]
        adjustments = sum(
            Decimal(r[k])
            for k in (
                "contractual_adjustment",
                "deductible_amount",
                "coinsurance_amount",
                "other_adjustment",
            )
        )
        assert Decimal(r["charge_amount"]) - adjustments == Decimal(r["paid_amount"])
        assert r["paid_amount"] == src["paid_amount"]
        assert r["deductible_amount"] == src["deductible_amount"]
        assert r["coinsurance_amount"] == src["coinsurance_amount"]
        assert r["payment_date"] == "2026-10-21"


def test_a_claim_allowed_nothing_is_denied():
    denied = [
        dict(
            CLAIMS[0],
            allowed_amount="0.00",
            paid_amount="0.00",
            coinsurance_amount="0.00",
            deductible_amount="0.00",
        )
    ]
    [_, r] = rows(gen.x12_835(denied, "20261021"), "835")
    line = dict(zip(COLUMNS_835, r, strict=True))
    assert (line["claim_status"], line["paid_amount"], line["contractual_adjustment"]) == (
        "4",
        "0.00",
        "200.00",
    )


def test_good_files_load_whole():
    assert codes(r := validate_x12(gen.x12_837(CLAIMS, "20261021"), C837, AT)) == []
    assert len(r.accepted) == 5
    assert codes(r := validate_x12(gen.x12_835(CLAIMS, "20261021"), C835, AT)) == []
    assert len(r.accepted) == 5


def _edit(data: bytes, old: str, new: str) -> bytes:
    text = data.decode()
    assert old in text
    return text.replace(old, new, 1).encode()


GOOD = gen.x12_837(CLAIMS, "20261021")


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "X12.INVALID"),
        (b"member_id,first_name\nM1,Alex\n" * 10, "X12.INVALID"),
        (GOOD[:50], "X12.INVALID"),
        (b"\xff\xfe" + GOOD, "FILE.ENCODING"),
        (apply(GOOD, [{"op": "x12_segment_lost"}]), "X12.ENVELOPE:SE01"),
        (_edit(GOOD, "SE*29*0001~", "SE*29*0009~"), "X12.ENVELOPE:SE02"),
        (_edit(GOOD, "GE*2*1~", "GE*3*1~"), "X12.ENVELOPE:GE01"),
        (_edit(GOOD, "GE*2*1~", "GE*2*7~"), "X12.ENVELOPE:GE02"),
        (_edit(GOOD, "IEA*1*000000001~", "IEA*2*000000001~"), "X12.ENVELOPE:IEA01"),
        (_edit(GOOD, "IEA*1*000000001~", "IEA*1*000000099~"), "X12.ENVELOPE:IEA02"),
        (_edit(GOOD, "IEA*1*000000001~\n", ""), "X12.ENVELOPE:IEA"),
        (_edit(GOOD, "GE*2*1~\n", ""), "X12.ENVELOPE:GE"),
        (_edit(GOOD, "SE*29*0001~\n", ""), "X12.ENVELOPE:SE"),
        (
            _edit(GOOD, "ST*837*0001*005010X222A1~", "ST*837*0001*005010X223A2~"),
            "X12.TRANSACTION:837",
        ),
        (gen.x12_835(CLAIMS, "20261021"), "X12.TRANSACTION:835"),
    ],
)
def test_envelope_problems_quarantine_the_whole_file(data, code):
    result = validate_x12(data, C837, AT)
    assert [v.reason_code for v in result.file_violations] == [code]
    assert result.accepted == []


def test_separators_come_from_the_isa_header():
    # The same interchange with other separators (| elements, ^ components... here ">"
    # components and "\n"-free "~" ends) reads the same.
    text = GOOD.decode()
    other = text.replace("*", "|").replace(":B:1", ">B>1").replace("ZZ:", "ZZ>")
    other = other[:104] + ">" + other[105:]
    assert rows(other.encode(), "837") == rows(GOOD, "837")


def test_a_bad_value_is_a_row_violation_with_the_usual_code():
    bad = [dict(r) for r in CLAIMS]
    bad[2]["service_date"] = "2025-13-40"  # written as 20251340: not a date
    result = validate_x12(gen.x12_837(bad, "20261021"), C837, AT)
    assert [(r.record["claim_line_id"][-6:], v.reason_code) for r, v in result.rejected] == [
        ("0002-1", "VALUE.TYPE:service_date")
    ]
    # 1 bad line of 5 is over the contract's 2%: the file is quarantined whole.
    assert [v.reason_code for v in result.file_violations] == ["FILE.BAD_ROW_RATIO"]


def test_an_adjustment_that_is_not_a_number_fails_its_column():
    data = _edit(gen.x12_835(CLAIMS, "20261021"), "CAS*CO*45*50.00~", "CAS*CO*45*5O.00~")
    result = validate_x12(data, C835, AT)
    assert [v.reason_code for _, v in result.rejected] == ["VALUE.TYPE:contractual_adjustment"]


def test_an_x12_contract_must_declare_the_parsers_columns(tmp_path):
    text = C837.path.read_text("utf-8").replace("  - { name: units,", "  - { name: unit_count,")
    with pytest.raises(ContractError, match="missing \\['units'\\], unknown \\['unit_count'\\]"):
        parse_contract(text, tmp_path / "claims_837.yaml")
    text = C837.path.read_text("utf-8").replace('transaction: "837", ', "")
    with pytest.raises(ContractError, match="transaction"):
        parse_contract(text, tmp_path / "claims_837.yaml")


REMIT = gen.x12_835(CLAIMS, "20261021")


@pytest.mark.parametrize("amount", ["sNaN", "NaN", "Infinity", "1E200000000", "20.004", "5O.00"])
def test_an_adjustment_that_is_not_a_plain_decimal_fails_its_line_never_the_task(amount):
    data = _edit(REMIT, "CAS*CO*45*50.00~", f"CAS*CO*45*{amount}~")
    result = validate_x12(data, C835, AT)
    # Not a number at all fails the type check; a number with three decimals, the pattern.
    [(_, v)] = result.rejected
    assert v.reason_code in ("VALUE.TYPE:contractual_adjustment",
                             "VALUE.PATTERN:contractual_adjustment")  # fmt: skip


def test_adjustments_are_summed_exactly():
    data = _edit(REMIT, "CAS*CO*45*50.00~", "CAS*CO*45*49.99*1*45*0.01~")
    lines = [dict(zip(COLUMNS_835, r, strict=True)) for r in rows(data, "835")[1:]]
    [line] = [r for r in lines if r["claim_line_id"].endswith("0001-1")]
    assert line["contractual_adjustment"] == "50.00"


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("CLP*00000000-0000-4000-8000-000000000001*1*245.50*152.00*38.00*15*PCN4193723377*11*1~\n"
         "NM1*QC*1*SUBSCRIBER*SYNTHETIC****MI*M000000001~\n",
         "CLP*00000000-0000-4000-8000-000000000001*1*245.50*152.00*38.00*15*PCN4193723377*11*1~\n"
         "NM1*QC*1*SUBSCRIBER*SYNTHETIC****MI*M000000001~\nCAS*OA*23*1.00~\n",
         "X12.UNSUPPORTED:CAS"),
        ("SE*29*0002~", "PLB*1999999984*20261231*WO:X*10.00~\nSE*30*0002~", "X12.UNSUPPORTED:PLB"),
    ],
)  # fmt: skip
def test_adjustments_this_reader_does_not_read_refuse_the_file(old, new, code):
    data = _edit(REMIT, old, new)
    if code.endswith("CAS"):  # one segment more in the transaction
        data = _edit(data, "SE*29*0002~", "SE*30*0002~")
    result = validate_x12(data, C835, AT)
    assert [v.reason_code for v in result.file_violations] == [code]


def test_a_subscriber_loop_without_its_member_does_not_borrow_the_previous_one():
    data = _edit(GOOD, "NM1*IL*1*SUBSCRIBER*SYNTHETIC****MI*M000000999~\n", "")
    data = _edit(data, "SE*29*0001~", "SE*28*0001~")
    result = validate_x12(data, C837, AT)
    assert [(r.record["claim_line_id"][-6:], v.reason_code) for r, v in result.rejected] == [
        ("0004-1", "VALUE.REQUIRED:member_id")
    ]


def test_an_other_payer_after_the_claim_does_not_replace_the_claims_payer():
    other = "REF*6R*00000000-0000-4000-8000-000000000003-1~\n"
    data = _edit(GOOD, other, other + "NM1*PR*2*OTHER PAYER*****PI*P99999~\n")
    data = _edit(data, "SE*29*0001~", "SE*30*0001~")
    table = rows(data, "837")
    assert {r[4] for r in table[1:]} == {"P45575", "P70055"}


def test_the_guide_version_is_read_from_gs08_when_st03_is_empty():
    data = _edit(GOOD, "ST*837*0001*005010X222A1~", "ST*837*0001~")
    assert validate_x12(data, C837, AT).file_violations == []
    data = _edit(data, "*X*005010X222A1~", "*X*004010X098A1~")
    assert [v.reason_code for v in validate_x12(data, C837, AT).file_violations] == [
        "X12.TRANSACTION:837"
    ]


def test_a_component_separator_that_is_a_letter_is_refused():
    text = GOOD.decode()
    bad = (text[:104] + "A" + text[105:]).encode()
    assert [v.reason_code for v in validate_x12(bad, C837, AT).file_violations] == ["X12.INVALID"]
