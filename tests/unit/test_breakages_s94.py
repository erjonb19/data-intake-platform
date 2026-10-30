"""Stage 9 (S9.4): the four new `dip break` scenarios."""

import pytest

from dip.breakages import SCENARIOS, apply

GOOD = b"member_id,dob,plan_code\nM1,1980-02-14,HMO\nM2,,PPO\n"


def test_reorder_columns_keeps_every_value_with_its_column():
    out = apply(GOOD, [{"op": "reorder_columns"}]).decode()
    assert out.splitlines() == ["plan_code,dob,member_id", "HMO,1980-02-14,M1", "PPO,,M2"]
    named = apply(GOOD, [{"op": "reorder_columns", "order": "dob,member_id,plan_code"}])
    assert named.decode().splitlines()[1] == "1980-02-14,M1,HMO"
    with pytest.raises(ValueError):
        apply(GOOD, [{"op": "reorder_columns", "order": "dob,member_id"}])


def test_type_change_rewrites_every_date_and_leaves_blanks():
    out = apply(GOOD, [{"op": "type_change", "column": "dob"}]).decode()
    assert out.splitlines()[1:] == ["M1,02/14/1980,HMO", "M2,,PPO"]


def test_truncated_pdf_and_late_file_work_on_bytes():
    pdf = b"%PDF-1.4\n" + b"x" * 91
    assert apply(pdf, [{"op": "truncated_pdf"}]) == pdf[:50]
    with pytest.raises(ValueError):
        apply(GOOD, [{"op": "truncated_pdf"}])
    assert apply(GOOD, [{"op": "late_file"}]) == GOOD  # its timing is what breaks
    assert {SCENARIOS[n].kind for n in ("reorder_columns", "late_file")} == {"tolerated", "timing"}
