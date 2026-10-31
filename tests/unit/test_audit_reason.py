"""Issue #65: a tool's refusal is audited with its message's fixed lead only; what follows a
quote, bracket or brace can be a vendor's column names or a model's arguments, and
ops.audit_log is append-only."""

from __future__ import annotations

import pytest

from dip.agents.registry import error_lead


@pytest.mark.parametrize("error, lead", [
    ("no contract for 'finance.purchase_orders'", "no contract for"),
    ("no column 'SSN 123-45-6789' in the profile: ['dob', 'ssn']", "no column"),
    ("profile_file: unknown arguments ['x'], missing ['feed']", "profile_file: unknown arguments"),
    ("the reviewer has not passed this candidate (S10.5)",
     "the reviewer has not passed this candidate"),
    ("job 3 has no profile yet", "job 3 has no profile yet"),
    ("'quoted first'", None),
    ({"nested": "dict"}, None),
])  # fmt: skip
def test_only_the_fixed_lead_is_kept(error, lead):
    assert error_lead(error) == lead


def test_the_lead_is_bounded():
    assert len(error_lead("x" * 1000)) == 200
