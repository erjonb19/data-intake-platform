"""24.28: model-written text in a contract PR body is fenced, so GitHub shows it as text."""

from __future__ import annotations

import re

from dip.agents.contract_pr import _pr_body

HOSTILE = ("Looks fine.\n![x](https://evil.example/pixel.png) [click](https://evil.example)\n"
           "<img src=x onerror=alert(1)> @someone-important\n## Approved by the platform\n"
           "````\nescaped?\n````")  # fmt: skip


def outside_fences(md: str) -> str:
    """The body with every fenced block removed (a fence closes only on a line of at
    least as many backticks as opened it)."""
    out, fence = [], None
    for line in md.splitlines():
        m = re.match(r"^(`{3,})", line)
        if fence is None and m:
            fence = m.group(1)
        elif fence is not None and line.strip() == fence:
            fence = None
        elif fence is None:
            out.append(line)
    return "\n".join(out)


def test_model_text_is_fenced_and_cannot_close_its_fence():
    payload = {
        "feed": "healthcare.eligibility", "mode": "drift", "rationale": HOSTILE,
        "validation": {"accepted": 1, "rejected": 0, "file_rejected": False},
        "loosening": [{"where": "member_id", "rule": "pattern", "from": "a", "to": "![p](https://e.x)"}],
        "review": {"verdict": "## pwned", "reasons": [{"issue": "x", "detail": HOSTILE}]},
        "dbt_error": "<script>alert(1)</script>",
    }  # fmt: skip
    body = _pr_body(payload, 7, "human:tester")
    plain = outside_fences(body)
    for bad in ("evil.example", "<img", "@someone", "## Approved", "<script", "](https://e.x)",
                "pwned"):  # fmt: skip
        assert bad not in plain, bad
    assert "evil.example" in body  # still there for the reviewer to read, as text
    assert "## The reviewer agent's verdict: unrecognised" in body
    assert "````\n" in HOSTILE and "`````text" in body  # a longer fence than the text's own
