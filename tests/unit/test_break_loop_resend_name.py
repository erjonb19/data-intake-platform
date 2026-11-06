"""Stage 12 (12.2, #101): the E2E break loop names the vendor's resend with the quarantined
file's business day, so the resend closes that day's entries even when the loop crosses
midnight UTC. The name must carry exactly that date under the eligibility contract."""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date
from pathlib import Path

from dip.contracts import load_contract

ROOT = Path(__file__).resolve().parents[2]
E2E = ROOT / "tests" / "e2e"
# mcp_read reads its URL and key at import; placeholders, since nothing here connects.
_SET = {k: "http://unused" for k in ("MCP_URL", "MCP_READER_KEY") if k not in os.environ}
os.environ.update(_SET)
sys.path.insert(0, str(E2E))
try:
    spec = importlib.util.spec_from_file_location("break_loop", E2E / "break_loop.py")
    break_loop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(break_loop)
finally:
    sys.path.remove(str(E2E))
    for k in _SET:
        del os.environ[k]

CONTRACT = load_contract(ROOT / "packs" / "healthcare" / "contracts" / "eligibility.yaml")


def test_the_resend_carries_the_quarantined_day_across_midnight():
    quarantined = date(2026, 11, 2)  # filed at 23:58 UTC
    received = date(2026, 11, 3)  # the resend lands after midnight
    name = break_loop.resend_name(quarantined)
    assert name == "elig_20261102_resend.csv"
    assert CONTRACT.matches_filename(name)
    assert CONTRACT.filename_date(name, received) == (quarantined, None)


def test_same_day_is_unchanged():
    day = date(2026, 11, 5)
    assert CONTRACT.filename_date(break_loop.resend_name(day), day) == (day, None)


def test_business_day_query_matches_the_resend_match() -> None:
    """`quarantined_business_day` must compute the day the way `_close_resent` matches it,
    or the E2E names the resend for a day the match never compares against."""
    import inspect
    import re

    from dip import warehouse

    def day_expr(src: str) -> str:
        found = re.search(r"coalesce\((.+?)::date\)", src.replace("d.", ""))
        assert found, "no business-day coalesce(...) found"
        return found.group(1)

    assert day_expr(inspect.getsource(break_loop.quarantined_business_day)) == day_expr(
        inspect.getsource(warehouse._close_resent)
    )
