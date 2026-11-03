"""Stage 11 (S11.11): the weekly live eval. A fixed sample of the recorded eval cases, two
per agent, runs live once and is compared, case by case, with the recorded baseline: drift
is a case that passed when recorded and fails live, or an agent costing more than twice its
baseline. PLAN.md section 8.13, design choice 5.

    python scripts/live_eval.py baseline        # replay the recordings: agents/evals/baseline.json
    python scripts/live_eval.py live OUT_DIR    # the sample live, then OUT_DIR/drift.md

Each case runs through its own eval harness and is scored by that harness's own scorer
(incident_evals, evals for triage, author_evals for the contract author through the intake
team, review_evals for the reviewer). Every model request goes through the S11.1 sink
(dip.agents.model_calls), which gives each case's calls, cost (`cost_usd`, the meter the
caps use) and latency. A replay has no latency (recordings keep none), so the baseline has
none and drift.md reports live latency alone.

Env: DIP_EVAL_DSN (a throwaway database whose name contains "eval" or "test"); for `live`,
ANTHROPIC_API_KEY and DIP_SPEND_CAP_USD (the whole run's cap, one process). Every input is
synthetic. Exit 1 on drift, or on a case that could not run.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

BASELINE = REPO / "agents" / "evals" / "baseline.json"
WORK = REPO / "data" / "tmp" / "live-eval"
# Two per agent: a plain case and one that is harder for the agent (a handoff, an
# injection, an onboarding, a planted defect). Fixed, so runs compare week to week.
SAMPLE: dict[str, list[str]] = {
    "incident": ["platform_oomkilled", "schema_drift_hands_off"],
    "triage": ["missing_column", "injection_in_a_cell"],
    "contract_author": ["drift_new_plan_code", "onboard_lab_results"],
    "reviewer": ["good_new_plan_code", "missed_phi"],
}
COST_FACTOR = 2.0  # an agent's live cost above twice its baseline is drift


@contextmanager
def collect(rows: list[dict[str, Any]]):
    """Every model call made inside the block, as model_calls rows."""
    from dip.agents import model_calls

    token = model_calls.SINK.set(rows.append)
    try:
        yield
    finally:
        model_calls.SINK.reset(token)


def _cases(agent: str) -> dict[str, dict[str, Any]]:
    from dip.agents import author_evals, evals, incident_evals, review_evals

    loaders: dict[str, Callable[[], list[dict[str, Any]]]] = {
        "incident": incident_evals.load_cases,
        "triage": evals.load_cases,
        "contract_author": author_evals.load_cases,
        "reviewer": review_evals.load_cases,
    }
    return {c["name"]: c for c in loaders[agent]()}


def run_one(agent: str, case: dict[str, Any], dsn: str, live: bool) -> Any:
    """The case through its own harness: live (AnthropicLLM) or replayed (its recordings).
    Returns the harness's score (each has .passed and .turns)."""
    from dip.agents import author_evals, evals, incident_evals, review_evals
    from dip.agents.llm import AnthropicLLM, Recording

    name = case["name"]

    def model(path: Path) -> Any:
        return AnthropicLLM() if live else Recording(path)

    if agent == "incident":
        return incident_evals.run_case(
            case, dsn,
            lambda step: model(incident_evals.RECORDINGS / f"{name}_{step}.json"),
            WORK / "incident")  # fmt: skip
    if agent == "triage":
        return evals.run_case(case, dsn, model(evals.RECORDINGS / f"{name}.json"), WORK / "triage")
    if agent == "contract_author":  # through the intake team: profile, map (map2), review
        runs: dict[str, int] = {}

        def llm_for(step: str) -> Any:
            runs[step] = runs.get(step, 0) + 1
            key = step + (str(runs[step]) if runs[step] > 1 else "")
            return model(author_evals.RECORDINGS / f"{name}_{key}.json")

        return author_evals.run_case(case, dsn, llm_for, WORK / "author")
    if agent == "reviewer":
        return review_evals.run_case(case, dsn, model(review_evals.RECORDINGS / f"{name}.json"),
                                     WORK / "reviewer")  # fmt: skip
    raise ValueError(f"unknown agent {agent!r}")


def measure(agent: str, case: dict[str, Any], dsn: str, live: bool) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    out: dict[str, Any] = {"agent": agent, "case": case["name"]}
    try:
        with collect(rows):
            score = run_one(agent, case, dsn, live)
        out["passed"], out["turns"] = bool(score.passed), int(getattr(score, "turns", 0) or 0)
    except Exception as exc:  # noqa: BLE001 - recorded on the row; the next case still runs
        out["passed"], out["error"] = False, f"{type(exc).__name__}: {exc}"[:400]
    out["calls"] = len(rows)
    out["cost_usd"] = round(sum(float(r.get("cost_usd") or 0) for r in rows), 6)
    if live:
        ms = [int(r.get("latency_ms") or 0) for r in rows]
        out["latency_ms_total"] = sum(ms)
        out["latency_ms_p50"] = int(statistics.median(ms)) if ms else 0
    return out


def sample(dsn: str, live: bool, sample_: dict[str, list[str]] = SAMPLE) -> list[dict[str, Any]]:
    results = []
    for agent, names in sample_.items():
        cases = _cases(agent)
        for name in names:
            r = measure(agent, cases[name], dsn, live)
            print(f"{'PASS' if r['passed'] else 'FAIL'} {agent}/{name}: {r.get('turns')} turns,"
                  f" {r['calls']} calls, ${r['cost_usd']:.4f}"
                  + (f", {r['error']}" if r.get("error") else ""), file=sys.stderr)  # fmt: skip
            results.append(r)
    return results


def compare(baseline: dict[str, Any], live: list[dict[str, Any]]) -> tuple[list[str], str]:
    """Drift findings, and drift.md. Drift: a case that passed in the baseline and fails
    live (or could not run), or an agent's live cost above COST_FACTOR x its baseline."""
    base = {(c["agent"], c["case"]): c for c in baseline["cases"]}
    findings: list[str] = []
    lines = ["# Live eval: drift against the recorded baseline", "",
             f"Baseline: {baseline.get('made_from', 'the recordings')}. Every input synthetic.",
             "", "| Agent | Case | Recorded | Live | Turns (rec / live) | Cost (rec / live) |"
             " Live latency p50 | Drift |", "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for r in live:
        b = base.get((r["agent"], r["case"]), {})
        drift = bool(b.get("passed")) and not r["passed"]
        if drift:
            findings.append(f"{r['agent']}/{r['case']}: passed when recorded, fails live"
                            + (f" ({r['error']})" if r.get("error") else ""))  # fmt: skip
        lines.append(
            f"| {r['agent']} | {r['case']} | {'pass' if b.get('passed') else 'fail'} |"
            f" {'pass' if r['passed'] else 'FAIL'} | {b.get('turns', '?')} / {r.get('turns')} |"
            f" ${b.get('cost_usd', 0):.4f} / ${r['cost_usd']:.4f} |"
            f" {r.get('latency_ms_p50', 0) / 1000:.1f} s |"
            f" {'**drift**' if drift else ''} |")  # fmt: skip
    lines += ["", "| Agent | Cost (recorded / live) | Calls (recorded / live) | Drift |",
              "|---|---|---|---|"]  # fmt: skip
    for agent in dict.fromkeys(r["agent"] for r in live):
        bc = sum(c["cost_usd"] for c in baseline["cases"] if c["agent"] == agent)
        lc = sum(r["cost_usd"] for r in live if r["agent"] == agent)
        bn = sum(c["calls"] for c in baseline["cases"] if c["agent"] == agent)
        ln = sum(r["calls"] for r in live if r["agent"] == agent)
        over = bc > 0 and lc > COST_FACTOR * bc
        if over:
            findings.append(f"{agent}: live cost ${lc:.4f} is over {COST_FACTOR:g}x the"
                            f" baseline's ${bc:.4f}")  # fmt: skip
        lines.append(f"| {agent} | ${bc:.4f} / ${lc:.4f} | {bn} / {ln} |"
                     f" {'**over 2x**' if over else ''} |")  # fmt: skip
    total = sum(r["cost_usd"] for r in live)
    lines += [
        "",
        f"Live spend, this run: ${total:.4f} (metered).",
        "",
        "Drift: " + ("none" if not findings else ""),
        *[f"- {f}" for f in findings],
    ]
    return findings, "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    dsn = os.environ["DIP_EVAL_DSN"]
    if argv[:1] == ["baseline"]:
        cases = sample(dsn, live=False)
        BASELINE.write_text(json.dumps({
            "made_from": "the committed recordings, replayed (scripts/live_eval.py baseline)",
            "sample": SAMPLE, "cases": cases}, indent=1) + "\n", "utf-8")  # fmt: skip
        print(f"wrote {BASELINE.relative_to(REPO)}")
        return 0 if all(c["passed"] for c in cases) else 1
    if argv[:1] == ["live"] and len(argv) == 2:
        out = Path(argv[1])
        out.mkdir(parents=True, exist_ok=True)
        baseline = json.loads(BASELINE.read_text("utf-8"))
        live = sample(dsn, live=True)
        findings, md = compare(baseline, live)
        (out / "drift.md").write_text(md, "utf-8")
        (out / "live.json").write_text(json.dumps(live, indent=1) + "\n", "utf-8")
        print(md)
        return 1 if findings else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
