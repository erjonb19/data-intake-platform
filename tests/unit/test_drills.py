"""Stage 11 (S11.6): `dip drill` runs only against the kind cluster, and each drill has an
undo; the pods' terminations are read as the platform check and get_k8s_events read them.
S11.8: bad_secret swaps the warehouse password and restores it, never in clear."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from dip import drills
from dip.k8s import terminations


class Kubectl:
    def __init__(self, context="kind-dip"):
        self.context, self.calls = context, []

    def __call__(self, *args, check=True):
        self.calls.append(args)
        return self.context if args[:2] == ("config", "current-context") else ""


def test_a_drill_refuses_any_cluster_but_kind():
    k = Kubectl(context="prod-eu")
    with pytest.raises(drills.DrillError, match="not the kind cluster"):
        drills.drill("scheduler_down", run=k)
    assert all(c[:2] == ("config", "current-context") for c in k.calls)  # nothing changed


def test_scheduler_down_and_its_undo():
    k = Kubectl()
    drills.drill("scheduler_down", run=k)
    assert ("-n", "dip-airflow", "scale", "deployment", "airflow-scheduler",
            "--replicas=0") in k.calls  # fmt: skip
    drills.drill("scheduler_down", undo=True, run=k)
    assert ("-n", "dip-airflow", "scale", "deployment", "airflow-scheduler",
            "--replicas=1") in k.calls  # fmt: skip


def test_an_unknown_drill():
    with pytest.raises(drills.DrillError, match="unknown drill"):
        drills.drill("delete_everything", run=Kubectl())


def test_terminations_name_the_task_and_why():
    pods = [{
        "metadata": {"name": "drill-oom-x1", "labels": {"dag_id": "drill_oom", "task_id": "eat"}},
        "spec": {"containers": [{"name": "base", "resources": {"limits": {"memory": "128Mi"}}}]},
        "status": {"containerStatuses": [{"name": "base", "state": {"terminated": {
            "reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-11-04T10:00:00Z"}}}]},
    }, {"metadata": {"name": "running"}, "status": {"containerStatuses": [
        {"name": "base", "state": {"running": {}}}]}}]  # fmt: skip
    [t] = terminations(pods)
    assert (t["reason"], t["exit_code"], t["dag_id"], t["task_id"]) == (
        "OOMKilled", 137, "drill_oom", "eat")  # fmt: skip
    assert t["limits"] == {"memory": "128Mi"}


def test_oom_task_runs_the_drill_dag_and_its_undo_pauses_it():
    k = Kubectl()
    drills.drill("oom_task", run=k)
    airflow_calls = [c[c.index("airflow") + 1 :] for c in k.calls if "airflow" in c]
    assert ("dags", "unpause", "drill_oom") in airflow_calls
    assert ("dags", "trigger", "drill_oom") in airflow_calls
    drills.drill("oom_task", undo=True, run=k)
    assert ("dags", "pause", "drill_oom") in [c[c.index("airflow") + 1:] for c in k.calls
                                               if "airflow" in c]  # fmt: skip
    assert any("delete" in c and "dag_id=drill_oom" in c for c in k.calls)


class SecretKubectl(Kubectl):
    """kubectl with Secrets, as `get -o json`, `apply -f` and `patch --patch-file` see them."""

    REAL = "postgresql://warehouse:real-pw-1@postgres.dip-data:5432/warehouse"

    def __init__(self):
        super().__init__()
        self.secrets = {"airflow-connections": {
            "metadata": {"name": "airflow-connections"},
            "data": {"warehouse": base64.b64encode(self.REAL.encode()).decode()}}}  # fmt: skip

    def __call__(self, *args, check=True):
        self.calls.append(args)
        if args[:2] == ("config", "current-context"):
            return self.context
        if "get" in args and "secret" in args:
            s = self.secrets.get(args[args.index("secret") + 1])
            return json.dumps(s) if s else ""
        if "apply" in args:
            doc = json.loads(Path(args[args.index("-f") + 1]).read_text("utf-8"))
            self.secrets[doc["metadata"]["name"]] = doc
        if "patch" in args:
            patch = json.loads(Path(args[args.index("--patch-file") + 1]).read_text("utf-8"))
            self.secrets[args[args.index("secret") + 1]]["data"] |= patch["data"]
        if "delete" in args and "secret" in args:
            self.secrets.pop(args[args.index("secret") + 1])
        return ""


def _warehouse(k):
    return base64.b64decode(k.secrets["airflow-connections"]["data"]["warehouse"]).decode()


def test_bad_secret_swaps_the_password_and_undo_restores_it():
    k = SecretKubectl()
    out = drills.drill("bad_secret", run=k)
    bad = _warehouse(k)
    assert bad.startswith("postgresql://warehouse:drill-") and bad.endswith(
        "@postgres.dip-data:5432/warehouse")  # fmt: skip
    assert "real-pw-1" not in bad and "real-pw-1" not in out and bad not in out
    assert drills.SAVED in k.secrets  # the real one, in a Secret of its own
    assert ("dags", "trigger", drills.FEED_DAG) in [c[c.index("airflow") + 1:] for c in k.calls
                                                    if "airflow" in c]  # fmt: skip
    with pytest.raises(drills.DrillError, match="already on"):  # never saves the bad one
        drills.drill("bad_secret", run=k)
    drills.drill("bad_secret", undo=True, run=k)
    assert _warehouse(k) == SecretKubectl.REAL and drills.SAVED not in k.secrets
    assert "nothing to restore" in drills.drill("bad_secret", undo=True, run=k)


def test_no_kubectl_argument_carries_a_password_even_encoded():
    # the values go in files; another process can read a command's arguments
    k = SecretKubectl()
    drills.drill("bad_secret", run=k)
    bad_pw = _warehouse(k).split(":")[2].split("@")[0]
    drills.drill("bad_secret", undo=True, run=k)
    held = [SecretKubectl.REAL, "real-pw-1", bad_pw]
    held += [base64.b64encode(s.encode()).decode() for s in held]
    assert not any(h in a for c in k.calls for a in c for h in held)


def test_stuck_queued_lowers_the_threshold_runs_the_dag_and_its_undo_restores_both():
    """S11.9: the drill saves the check's threshold on the CronJob, lowers it, and runs
    drill_queue; the undo pauses it, deletes its run and pods, restarts the scheduler (its
    executor would otherwise keep the Pending task's slot) and puts the threshold back."""

    class Cron(Kubectl):
        def __init__(self):
            super().__init__()
            self.annotations, self.env = {}, "300"

        def __call__(self, *args, check=True):
            out = super().__call__(*args, check=check)
            if "jsonpath={.metadata.annotations}" in args:
                return json.dumps(self.annotations) if self.annotations else ""
            if any(a.startswith("jsonpath={.spec.jobTemplate") for a in args):
                return self.env
            if "annotate" in args:
                k, _, v = args[-2 if args[-1] == "--overwrite" else -1].partition("=")
                if k.endswith("-"):
                    self.annotations.pop(k[:-1], None)
                else:
                    self.annotations[k] = v
            if "set" in args and "env" in args:
                self.env = args[-1].split("=", 1)[1]
            return out

    k = Cron()
    drills.drill("stuck_queued", run=k)
    assert k.env == "120" and k.annotations == {"dip.drill/queued-stale-seconds": "300"}
    airflow = [c[c.index("airflow") + 1 :] for c in k.calls if "airflow" in c]
    assert ("dags", "unpause", "drill_queue") in airflow
    assert ("dags", "trigger", "drill_queue") in airflow
    with pytest.raises(drills.DrillError, match="already on"):
        drills.drill("stuck_queued", run=k)
    drills.drill("stuck_queued", undo=True, run=k)
    assert k.env == "300" and k.annotations == {}
    airflow = [c[c.index("airflow") + 1 :] for c in k.calls if "airflow" in c]
    assert ("dags", "pause", "drill_queue") in airflow
    assert ("dags", "delete", "drill_queue", "--yes") in airflow
    assert any("delete" in c and "dag_id=drill_queue" in c for c in k.calls)
    assert ("-n", "dip-airflow", "rollout", "restart", "deployment/airflow-scheduler") in k.calls
    drills.drill("stuck_queued", undo=True, run=k)  # twice: nothing left to restore
    assert k.env == "300"


class _Flaky(Kubectl):
    """kubectl that fails on one kind of call."""

    def __init__(self, fail_on):
        super().__init__()
        self.fail_on, self.env, self.annotations = fail_on, "300", {}

    def __call__(self, *args, check=True):
        out = super().__call__(*args, check=check)
        if args[:2] == ("config", "current-context"):
            return out
        if self.fail_on in args and check:
            raise drills.DrillError(f"kubectl {self.fail_on}: failed")
        if "jsonpath={.metadata.annotations}" in args:
            return json.dumps(self.annotations) if self.annotations else ""
        if "annotate" in args:
            k, _, v = args[-2 if args[-1] == "--overwrite" else -1].partition("=")
            if k.endswith("-"):
                self.annotations.pop(k[:-1], None)
            else:
                self.annotations[k] = v
        if "set" in args and "env" in args:
            self.env = args[-1].split("=", 1)[1]
        return ""


def test_stuck_queued_leaves_the_check_alone_when_the_dag_is_not_there():
    k = _Flaky("unpause")  # drill_queue not parsed yet
    with pytest.raises(drills.DrillError):
        drills.drill("stuck_queued", run=k)
    assert k.env == "300" and k.annotations == {}


def test_stuck_queued_undo_restores_the_threshold_even_if_the_restart_fails():
    k = _Flaky("restart")
    drills.drill("stuck_queued", run=k)
    assert k.env == "120"
    with pytest.raises(drills.DrillError):
        drills.drill("stuck_queued", undo=True, run=k)
    assert k.env == "300" and k.annotations == {}


class _Runs(_Flaky):
    """The CLI answers: drill_queue is listed (after a log line) and still has a run."""

    def __call__(self, *args, check=True):
        out = super().__call__(*args, check=check)
        if "list-runs" in args:
            return '[{"run_id": "manual__1"}]'
        if args[-4:] == ("dags", "list", "-o", "json"):
            return '[2026-11-03] INFO - a log line\n[{"dag_id": "drill_queue"}]'
        return out


def test_stuck_queued_undo_fails_when_the_runs_survive():
    with pytest.raises(drills.DrillError, match="not deleted"):
        drills.drill("stuck_queued", undo=True, run=_Runs("nothing"))


def test_stuck_queued_undo_fails_when_the_cli_cannot_answer():
    """Review of S11.9: a list-runs that fails is not taken for "the runs are gone"."""
    with pytest.raises(drills.DrillError, match="list-runs"):
        drills.drill("stuck_queued", undo=True, run=_Runs("list-runs"))


def test_stuck_queued_undo_passes_when_the_dag_is_gone_or_has_no_runs():
    class Empty(_Flaky):
        def __call__(self, *args, check=True):
            out = super().__call__(*args, check=check)
            return "No data found" if args[-4:] == ("dags", "list", "-o", "json") else out

    drills.drill("stuck_queued", undo=True, run=Empty("nothing"))  # no error


def test_cli_output_that_is_not_json_is_a_drill_error():
    with pytest.raises(drills.DrillError, match="not JSON"):
        drills._json_list("[not json")
