"""Stage 0 end-to-end smoke test against a running stack.

Checks: MinIO is healthy, the Airflow API answers, and the platform_smoke DAG
succeeds with every task in its own KubernetesExecutor pod, and its task logs
can be read back from remote storage after the pods are gone.

Standard library only, so it runs in CI and on a laptop without extra installs.

Env: AIRFLOW_URL, AIRFLOW_USER, AIRFLOW_PASSWORD, MINIO_URL, SMOKE_TIMEOUT (seconds).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

AIRFLOW_URL = os.environ.get("AIRFLOW_URL", "http://localhost:8080").rstrip("/")
MINIO_URL = os.environ.get("MINIO_URL", "http://localhost:9000").rstrip("/")
USER = os.environ.get("AIRFLOW_USER", "admin")
PASSWORD = os.environ["AIRFLOW_PASSWORD"]
TIMEOUT = int(os.environ.get("SMOKE_TIMEOUT", "600"))
DAG_ID = "platform_smoke"
EXPECTED_TASKS = {"check_minio", "check_warehouse", "report"}


def request(method: str, url: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)


def wait_for(description: str, fn, timeout: int = TIMEOUT, interval: float = 5.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {description} (last error: {last_error})")


def step(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def main() -> int:
    step(f"MinIO health at {MINIO_URL}")
    wait_for("MinIO ready", lambda: request("GET", f"{MINIO_URL}/minio/health/ready")[0] == 200)

    step(f"Airflow token from {AIRFLOW_URL}")

    def get_token():
        status, body = request(
            "POST", f"{AIRFLOW_URL}/auth/token", {"username": USER, "password": PASSWORD}
        )
        return body["access_token"] if status in (200, 201) else None

    token = wait_for("Airflow API token", get_token)
    api = f"{AIRFLOW_URL}/api/v2"

    def dag_parsed():
        try:
            return request("GET", f"{api}/dags/{DAG_ID}", token=token)[0] == 200
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    step(f"waiting for DAG {DAG_ID} to be parsed")
    wait_for("DAG parsed", dag_parsed)
    request("PATCH", f"{api}/dags/{DAG_ID}", {"is_paused": False}, token=token)

    step("triggering DAG run")
    _, run = request("POST", f"{api}/dags/{DAG_ID}/dagRuns", {"logical_date": None}, token=token)
    run_id = run["dag_run_id"]
    print(f"    run_id={run_id}")

    def run_finished():
        _, body = request("GET", f"{api}/dags/{DAG_ID}/dagRuns/{run_id}", token=token)
        return body["state"] if body["state"] in ("success", "failed") else None

    state = wait_for("DAG run to finish", run_finished)
    _, tis = request("GET", f"{api}/dags/{DAG_ID}/dagRuns/{run_id}/taskInstances", token=token)
    instances = tis["task_instances"]
    for ti in instances:
        print(f"    {ti['task_id']:<16} {ti['state']:<8} pod={ti.get('hostname')}")

    failures: list[str] = []
    if state != "success":
        failures.append(f"DAG run state is {state!r}, expected 'success'")
    if {ti["task_id"] for ti in instances} != EXPECTED_TASKS:
        failures.append(f"unexpected task set: {sorted(ti['task_id'] for ti in instances)}")
    hostnames = {ti.get("hostname") for ti in instances}
    if len(hostnames) != len(instances) or None in hostnames:
        failures.append(f"tasks did not each run in their own pod: {hostnames}")

    step("reading task log back from remote storage")
    # The task pod uploads its log to MinIO as it exits, which can lag the task's
    # "success" state by a few seconds, so poll rather than read once.
    log_errors: list[str] = []

    def log_has_output():
        try:
            status, log = request(
                "GET",
                f"{api}/dags/{DAG_ID}/dagRuns/{run_id}/taskInstances/report/logs/1",
                token=token,
            )
        except urllib.error.HTTPError as exc:
            log_errors.append(f"HTTP {exc.code}")
            return None
        return status == 200 and "warehouse checked in pod" in json.dumps(log)

    try:
        wait_for("report task log in remote storage", log_has_output, timeout=120)
    except TimeoutError:
        detail = f" (last error: {log_errors[-1]})" if log_errors else ""
        failures.append(f"report task log is missing expected output{detail}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("PASS: Stage 0 smoke test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
