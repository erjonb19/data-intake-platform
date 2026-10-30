"""Stage 10 (S10.3): the dbt models generated from a new feed's contract build and pass
their tests, on rows the real intake loaded (needs dbt: CI's test job has it)."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from dip import BUCKET_LANDING, dbt_gen
from dip.contracts import REPO_ROOT, load_contracts, parse_contract
from dip.dbt_runner import run_dbt
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.integration.conftest import DSN, rows

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dbt_gen"
FEED = "finance.purchase_orders"


def test_generated_models_build_and_keep_the_newest_row_per_key(warehouse, tmp_path):
    path = FIXTURES / "purchase_orders.yaml"
    contract = parse_contract(path.read_text(encoding="utf-8"), path)
    contracts = {**load_contracts(), FEED: contract}
    store = LocalStore(tmp_path)
    for name, at in (("po_20261002.csv", datetime(2026, 10, 2, 6, tzinfo=UTC)),
                     ("po_20261003.csv", datetime(2026, 10, 3, 6, tzinfo=UTC))):  # fmt: skip
        store.put(BUCKET_LANDING, f"{FEED}/{name}", (FIXTURES / name).read_bytes())
        (o,) = process_landing(store, warehouse, contracts, at)
        assert o.status == "loaded", o
    files = dbt_gen.plan_files(contract)
    assert len(files) == 3
    folder = REPO_ROOT / "dbt" / "models" / "finance" / "purchase_orders"
    assert not folder.exists(), "a real purchase_orders model folder would be overwritten"
    try:
        dbt_gen.write(files)
        assert run_dbt(DSN, "build", "--select", f"tag:{FEED}", check=False).returncode == 0
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    assert rows(warehouse, "select count(*) from silver.finance_purchase_orders_record") == [(5,)]
    got = rows(warehouse, "select po_number, line, amount, approved, note, due_date::text"
                          " from gold.finance_purchase_orders order by 1, 2")  # fmt: skip
    assert got == [
        ("PO-0001", 1, Decimal("125.00"), True, "amended", None),  # the newer file's row
        ("PO-0001", 2, Decimal("30.00"), True, "rush", "2026-11-01"),
        ("PO-0002", 1, Decimal("99.99"), False, None, None),
        ("PO-0003", 1, Decimal("10.00"), False, None, None),
    ]
