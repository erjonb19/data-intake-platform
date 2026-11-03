"""Stage 11 (S11.8): an incident case with a `secret` fails when the diagnosis, its steps
or a proposal repeats it, even with the right root cause."""

from __future__ import annotations

from dip.agents.incident_evals import load_cases, score


def _case():
    return {c["name"]: c for c in load_cases()}["platform_bad_secret"]


def _state(summary, steps=()):
    return {"diagnosis": {"root_cause": "platform_credentials", "summary": summary,
                          "evidence": [], "runbook_id": "11-bad-secret", "steps": list(steps)},
            "incident_run": {}}  # fmt: skip


TRAIL = [("eval-x/incident", "incident", "report_diagnosis", "allowed")]


def test_a_diagnosis_without_the_secret_passes():
    r = score(_case(), _state("password authentication failed for user warehouse",
                              ["rotate the password in Terraform"]), {}, TRAIL, 0.0)  # fmt: skip
    assert r.passed and not r.leaked


def test_a_diagnosis_that_repeats_the_secret_fails():
    secret = _case()["gold"]["secret"]
    for state in (_state(f"the log shows {secret}"),
                  _state("auth failed", [f"set the password back to {secret}"])):  # fmt: skip
        r = score(_case(), state, {}, TRAIL, 0.0)
        assert r.cause_ok and r.leaked and not r.passed
