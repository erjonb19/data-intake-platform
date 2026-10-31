"""Stage 10 (S10.6): the stage's proof on a running stack, with a model key. Run by the
manual `proof` workflow (.github/workflows/proof.yml) after up.sh, smoke.sh and e2e.sh;
the stack runs without a model key until here, so the E2E spends nothing.

    python scripts/proof_intake.py OUT_DIR

1. The E2E queued one onboarding job, for a new vendor's sample of
   finance.purchase_orders (tests/e2e/intake_jobs.py). It must be the oldest queued job,
   so the runner's one claim is that job.
2. The intake team runs on it once, in the cluster: a pod of the platform image runs
   `python -m dip.agents.intake_runner --limit 1` in the agent venv with the agent_chain
   DAG's Secret, plus the model key (a Secret made for the run and deleted after) and
   DIP_SPEND_CAP_USD, so no request is sent that could pass the cap (dip.agents.llm).
3. Checked: the job ends `proposed`; every step is in ops.audit_log under the job's one
   chain id; the proposal is pending and no branch exists before a person approves.
4. Screenshots of the proposal and its chain on the approvals page (scripts/screenshots.py,
   SHOTS=intake).
5. Approved and executed with `dip approvals`, as a person would (the person here is
   whoever started the workflow); the branch must hold the contract and the generated dbt
   models, and `dbt build` must pass on those models against the proof stack's warehouse.
6. OUT_DIR/results.md: each check, the job, the chain, the spend.

Env: WAREHOUSE_DSN, KUBECONFIG, AIRFLOW_IMAGE (dip-airflow:<tag>), ANTHROPIC_API_KEY,
DIP_SPEND_CAP_USD (default 0.25), APPROVALS_TOKEN, CHROME (for the screenshots).
Every input is synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
FEED = "finance.purchase_orders"
NS = "dip-airflow"
POD = "intake-proof"
TEAM = {"profile", "contract_author", "validator", "reviewer", "intake_orchestrator"}


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    out = subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True)
    if check and out.returncode:
        raise RuntimeError(f"kubectl {args[0]}: {out.stderr.strip()[-500:]}")
    return out.stdout


def dip(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DIP_WAREHOUSE_DSN": os.environ["WAREHOUSE_DSN"], "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, "-m", "dip", *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=180)  # fmt: skip


def run_team(cap: str) -> dict:
    """One claim, in the cluster; the model key lives in a Secret only for the run."""
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(os.environ["ANTHROPIC_API_KEY"])
    try:
        kubectl("-n", NS, "create", "secret", "generic", "dip-proof-model",
                f"--from-file=ANTHROPIC_API_KEY={f.name}")  # fmt: skip
    finally:
        os.unlink(f.name)
    container = {
        "name": POD,
        "image": os.environ["AIRFLOW_IMAGE"],
        "command": [
            "/opt/airflow/agent-venv/bin/python",
            "-m",
            "dip.agents.intake_runner",
            "--limit",
            "1",
            "--dag-run-id",
            "s10.6-proof",
        ],  # fmt: skip
        # The later source wins: the run's key over the DAG Secret's empty one.
        "envFrom": [
            {"secretRef": {"name": "dip-agent-chain"}},
            {"secretRef": {"name": "dip-proof-model"}},
        ],  # fmt: skip
        "env": [
            {"name": "DIP_SPEND_CAP_USD", "value": cap},
            {
                "name": "DIP_WAREHOUSE_DSN",
                "valueFrom": {"secretKeyRef": {"name": "airflow-connections", "key": "warehouse"}},
            },
        ],  # fmt: skip
        # The agent_chain DAG's task limits.
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "1", "memory": "512Mi"},
        },  # fmt: skip
    }
    overrides = {"apiVersion": "v1", "spec": {"containers": [container]}}
    try:
        kubectl("-n", NS, "run", POD, "--restart=Never", f"--image={os.environ['AIRFLOW_IMAGE']}",
                f"--overrides={json.dumps(overrides)}")  # fmt: skip
        phase = ""
        for _ in range(180):  # 15 minutes
            phase = kubectl("-n", NS, "get", "pod", POD, "-o", "jsonpath={.status.phase}")
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(5)
        logs = kubectl("-n", NS, "logs", POD, check=False)
        print(logs[-4000:])
        lines = [ln for ln in logs.splitlines() if ln.startswith("{")]
        summary = json.loads(lines[-1]) if lines else {}
        summary["pod_phase"] = phase
        return summary
    finally:
        kubectl("-n", NS, "delete", "pod", POD, "--ignore-not-found", "--wait=false", check=False)
        kubectl("-n", NS, "delete", "secret", "dip-proof-model", "--ignore-not-found", check=False)


def main() -> int:
    import psycopg

    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    cap = os.environ.get("DIP_SPEND_CAP_USD", "0.25")
    rows: list[tuple[str, bool, str]] = []

    def check(ok: bool, what: str, detail: str = "") -> bool:
        print(f"==> {what}: {'ok' if ok else 'FAILED'} {detail}")
        rows.append((what, ok, detail))
        return ok

    def q(sql: str, *params):
        with psycopg.connect(os.environ["WAREHOUSE_DSN"]) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    first = q("select job_id, feed, mode from ops.intake_job where status = 'queued'"
              " order by created_at, job_id limit 1")  # fmt: skip
    job = first[0] if first else None
    if not check(bool(job) and job[1:] == (FEED, "onboard"),
                 "the oldest queued job is the E2E's onboarding job", str(job)):  # fmt: skip
        return finish(out, rows, {})
    job_id = job[0]

    summary = run_team(cap)
    spent = float(summary.get("spent_usd", 0))
    check(summary.get("done", {}).get(str(job_id)) == "proposed" and summary["claimed"] == 1,
          "the team claimed that one job and proposed", json.dumps(summary))  # fmt: skip
    check(spent <= float(cap), f"spend within the ${float(cap):.2f} cap", f"${spent:.4f}")

    status, chain, approval = q("select status, chain_id, approval_id from ops.intake_job"
                                " where job_id = %s", job_id)[0]  # fmt: skip
    info = {
        "job_id": job_id,
        "chain_id": chain,
        "approval_id": approval,
        "spent_usd": spent,
        "provider": q("select provider from ops.intake_job where job_id = %s", job_id)[0][0],
    }
    check(status == "proposed" and approval is not None, "the job ended proposed", status)
    if approval is None:
        return finish(out, rows, info)
    audit = q("select principal, run_id, tool, decision from ops.audit_log where chain_id = %s"
              " order by audit_id", chain)  # fmt: skip
    principals = {r[0] for r in audit}
    check(principals >= TEAM, "every step is in the audit log under the one chain id",
          f"{len(audit)} calls by {sorted(principals)}")  # fmt: skip
    info["audit_calls"] = len(audit)
    (out / "audit.json").write_text(json.dumps(audit, indent=1), "utf-8")
    appr = q("select status, action, payload from ops.approvals where approval_id = %s", approval)
    payload = appr[0][2]
    branch = f"contract/{FEED}-approval-{approval}"
    before = subprocess.run(["git", "branch", "--list", branch], cwd=REPO, capture_output=True,
                            text=True).stdout.strip()  # fmt: skip
    check(appr[0][:2] == ("pending", "contract_pr") and not before,
          "nothing written before the approval: pending, no branch", appr[0][0])  # fmt: skip
    info["verdict"] = (payload.get("review") or {}).get("verdict")

    env = {**os.environ, "SHOTS": "intake", "APPROVAL_ID": str(approval), "CHAIN": chain}
    shots = subprocess.run([sys.executable, str(REPO / "scripts/screenshots.py"), str(out)],
                           env=env, capture_output=True, text=True)  # fmt: skip
    check(shots.returncode == 0 and (out / "intake-proposal.png").exists(),
          "screenshots of the proposal and its chain", shots.stderr.strip()[-300:])  # fmt: skip

    r = dip("approvals", "approve", str(approval), "--note", "S10.6 proof workflow")
    check(r.returncode == 0, "approved", (r.stdout + r.stderr).strip()[-300:])
    r = dip("approvals", "execute", str(approval))
    check(r.returncode == 0, "executed: the branch is written",
          (r.stdout + r.stderr).strip()[-300:])  # fmt: skip
    files = subprocess.run(["git", "diff", "--name-only", f"main...{branch}"], cwd=REPO,
                           capture_output=True, text=True).stdout.split()  # fmt: skip
    info["branch"], info["files"] = branch, files
    contract = f"packs/finance/contracts/{FEED.split('.')[1]}.yaml"
    models = [f for f in files if f.startswith("dbt/models/")]
    check(contract in files and len(models) >= 2, "the branch holds the contract and its models",
          ", ".join(files))  # fmt: skip
    if models:
        check(*dbt_build(branch, models))
    return finish(out, rows, info)


def dbt_build(branch: str, models: list[str]) -> tuple[bool, str, str]:
    """`dbt build` of the branch's generated models (and their tests) on the warehouse."""
    dsn = urlparse(os.environ["WAREHOUSE_DSN"])
    tree = Path(tempfile.mkdtemp()) / "branch"
    subprocess.run(["git", "worktree", "add", "--detach", str(tree), branch], cwd=REPO,
                   check=True, capture_output=True)  # fmt: skip
    env = {**os.environ, "DIP_PG_HOST": dsn.hostname or "localhost",
           "DIP_PG_PORT": str(dsn.port or 5432), "DIP_PG_USER": dsn.username or "",
           "DIP_PG_PASSWORD": dsn.password or "", "DIP_PG_DB": dsn.path.lstrip("/")}  # fmt: skip
    folder = str(Path(models[0]).parent.relative_to("dbt"))
    dbt = os.environ.get("DBT", "dbt")
    r = subprocess.run([dbt, "build", "--select", f"path:{folder}", "--profiles-dir", "."],
                       cwd=tree / "dbt", env=env, capture_output=True, text=True)  # fmt: skip
    subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=REPO, check=False)
    tail = [ln for ln in r.stdout.splitlines() if "Done." in ln or "ERROR" in ln]
    return r.returncode == 0, f"dbt build passes on {folder}", " ".join(tail)[-300:]


def finish(out: Path, rows: list[tuple[str, bool, str]], info: dict) -> int:
    ok = all(r[1] for r in rows)
    lines = [f"# S10.6 proof: {'PASS' if ok else 'FAIL'}", "", "| Check | Result | Detail |",
             "|---|---|---|"]  # fmt: skip
    for what, passed, detail in rows:
        cell = detail.replace("|", "/").replace("\n", " ")[:300]
        lines.append(f"| {what} | {'ok' if passed else 'FAILED'} | {cell} |")
    lines += ["", "```json", json.dumps(info, indent=1, default=str), "```", ""]
    (out / "results.md").write_text("\n".join(lines), "utf-8")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
