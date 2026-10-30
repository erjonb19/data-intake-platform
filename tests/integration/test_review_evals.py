"""Stage 10 (S10.5): the reviewer's recorded evals replay (no model call), and every seeded
candidate is a valid contract that accepts its world's file, so a verdict on it is about
judgment, not a broken candidate. The candidate files are only contracts: no comment in
them may say what the case expects."""

from __future__ import annotations

import pytest

from dip.agents import author_evals, review_evals
from dip.agents.llm import Recording
from dip.contract_author import dry_run
from dip.contracts import REPO_ROOT, parse_contract
from tests.integration.conftest import DSN

CASES = review_evals.load_cases()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_the_candidate_is_valid_accepts_its_file_and_gives_nothing_away(case):
    text = (review_evals.EVALS / "candidates" / f"{case['candidate']}.yaml").read_text("utf-8")
    assert not text.startswith("#"), "a candidate must not start with a comment"
    for word in (
        "faulty",
        "good_",
        "missed",
        "loosened",
        "unsupported",
        "reference contract",
        "eval",
    ):
        assert word not in text.lower(), word
    world = {c["name"]: c for c in author_evals.load_cases()}[case["world"]]
    c = parse_contract(text, REPO_ROOT / "candidate.yaml")
    name, data = author_evals.case_file(world)
    run = dry_run(c, data, author_evals.INGEST_AT, name)
    assert not run.get("file_rejected"), run


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_recorded_review(case, tmp_path):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    """The replay reproduces the recorded run's verdict. The recorded run is one live run,
    scored as it came out (results/recorded.json: one good candidate sent back); the
    reviewer's figures are its 3x live variance (results/variance.json)."""
    import json

    path = review_evals.RECORDINGS / f"{case['name']}.json"
    r = review_evals.run_case(case, DSN, Recording(path), tmp_path)
    recorded = json.loads((review_evals.EVALS / "results" / "recorded.json").read_text("utf-8"))
    want = next(c for c in recorded["cases"] if c["case"] == case["name"])
    assert r.verdict == want["verdict"] and not r.violations, r
