"""Writes the finance fixtures (python tests/fixtures/finance/make_fixtures.py).

Generated, invented invoice registers from packs/finance/generators/invoices.py with a
fixed seed and date, so rerunning this gives byte-identical files. Names carry no date, so
the E2E can land them on any day (the received date stands in for the business date).
"""

from __future__ import annotations

import importlib.util
import io
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
DAY = date(2026, 10, 19)
# A register of the day before: invoice numbers are per day, so two of one day would clash.
BROKEN_DAY = date(2026, 10, 18)


def _gen():
    path = ROOT / "packs/finance/generators/invoices.py"
    spec = importlib.util.spec_from_file_location("invoices_gen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    gen = _gen()
    good = gen.invoice_rows(DAY, 12, seed=7)
    broken = gen.apply_breakages(
        gen.invoice_rows(BROKEN_DAY, 40, seed=8),
        ["duplicate", "negative_total", "missing_po", "total_mismatch"],
    )
    files = {
        "good/invoices_good.xlsx": gen.workbook_bytes(good, DAY),
        "bad/invoices_breakages.xlsx": gen.workbook_bytes(broken, BROKEN_DAY),
        "bad/invoices_sheet_renamed.xlsx": _renamed(gen.workbook_bytes(good[:3], DAY), gen),
    }
    for name, data in files.items():
        path = HERE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {name}")


def _renamed(data: bytes, gen) -> bytes:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data))
    wb.active.title = "Sheet1"
    wb.properties.created = wb.properties.modified = gen.FIXED
    buf = io.BytesIO()
    wb.save(buf)
    return gen._deterministic(buf.getvalue())


if __name__ == "__main__":
    main()
