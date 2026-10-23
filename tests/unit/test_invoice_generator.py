"""The finance invoice generator (S2.5): deterministic workbooks, totals that add up, and
breakages that give the reason codes they promise."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dip.contracts import REPO_ROOT, load_contracts
from dip.excelfeed import validate_excel

DAY = date(2026, 10, 19)
RECEIVED = datetime(2026, 10, 20, tzinfo=UTC)


@pytest.fixture(scope="module")
def gen():
    path = REPO_ROOT / "packs/finance/generators/invoices.py"
    spec = importlib.util.spec_from_file_location("invoices_gen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(gen, rows):
    contract = load_contracts()["finance.vendor_invoice"]
    return validate_excel(gen.workbook_bytes(rows, DAY), contract, RECEIVED)


def test_same_seed_and_date_give_the_same_bytes(gen):
    a = gen.workbook_bytes(gen.invoice_rows(DAY, 20, 7), DAY)
    assert a == gen.workbook_bytes(gen.invoice_rows(DAY, 20, 7), DAY)
    assert a != gen.workbook_bytes(gen.invoice_rows(DAY, 20, 8), DAY)


def test_clean_register_is_valid_and_adds_up(gen):
    rows = gen.invoice_rows(DAY, 40, 7)
    r = check(gen, rows)
    assert not r.file_violations and not r.rejected and len(r.accepted) == 40
    for row in rows:
        assert row["subtotal"] + row["tax"] == row["total"]
        assert row["due_date"] >= row["invoice_date"]
    assert len({row["invoice_number"] for row in rows}) == 40


def test_invoice_numbers_differ_across_days(gen):
    a = {r["invoice_number"] for r in gen.invoice_rows(DAY, 40, 7)}
    b = {r["invoice_number"] for r in gen.invoice_rows(date(2026, 10, 20), 40, 7)}
    assert not a & b


@pytest.mark.parametrize(
    ("breakage", "codes"),
    [
        ("duplicate", ["KEY.DUPLICATE:invoice_number", "KEY.DUPLICATE:invoice_number"]),
        ("negative_total", ["VALUE.OUT_OF_RANGE:total"]),
        ("missing_po", ["VALUE.REQUIRED:po_number"]),
        ("total_mismatch", []),  # passes the contract; dbt's invoice_totals_add_up finds it
    ],
)
def test_breakages(gen, breakage, codes):
    r = check(gen, gen.apply_breakages(gen.invoice_rows(DAY, 40, 7), [breakage]))
    assert sorted(v.reason_code for _, v in r.rejected) == codes


def test_unknown_breakage(gen):
    with pytest.raises(ValueError, match="unknown breakage"):
        gen.apply_breakages(gen.invoice_rows(DAY, 5, 7), ["nope"])


def test_total_mismatch_is_off_by_ten(gen):
    rows = gen.apply_breakages(gen.invoice_rows(DAY, 10, 7), ["total_mismatch"])
    assert rows[4]["total"] - rows[4]["subtotal"] - rows[4]["tax"] == Decimal("10.00")
