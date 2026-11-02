"""Stage 11 (S11.6): `dip drill` runs only against the kind cluster, and each drill has an
undo; the pods' terminations are read as the platform check and get_k8s_events read them.
S11.8: bad_secret swaps the warehouse password and restores it, never in clear."""

from __future__ import annotations

import base64
import json

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
    """kubectl with the airflow-connections Secret, as `get -o json` returns it."""

    def __init__(self, uri="postgresql://warehouse:real-pw-1@postgres.dip-data:5432/warehouse"):
        super().__init__()
        self.secret = {"metadata": {"name": "airflow-connections"},
                       "data": {"warehouse": base64.b64encode(uri.encode()).decode()}}  # fmt: skip

    def __call__(self, *args, check=True):
        self.calls.append(args)
        if args[:2] == ("config", "current-context"):
            return self.context
        if "get" in args and "secret" in args:
            return json.dumps(self.secret)
        if "patch" in args:
            patch = json.loads(args[args.index("-p") + 1])
            ann = self.secret["metadata"].setdefault("annotations", {})
            for k, v in patch.get("metadata", {}).get("annotations", {}).items():
                ann.pop(k) if v is None else ann.__setitem__(k, v)
            self.secret["data"] |= patch.get("data", {})
        return ""


def _warehouse(k):
    return base64.b64decode(k.secret["data"]["warehouse"]).decode()


def test_bad_secret_swaps_the_password_and_undo_restores_it():
    k = SecretKubectl()
    out = drills.drill("bad_secret", run=k)
    bad = _warehouse(k)
    assert bad.startswith("postgresql://warehouse:drill-") and bad.endswith(
        "@postgres.dip-data:5432/warehouse")  # fmt: skip
    assert "real-pw-1" not in bad and "real-pw-1" not in out and bad not in out
    assert ("dags", "trigger", drills.FEED_DAG) in [c[c.index("airflow") + 1:] for c in k.calls
                                                    if "airflow" in c]  # fmt: skip
    with pytest.raises(drills.DrillError, match="already on"):  # never saves the bad one
        drills.drill("bad_secret", run=k)
    drills.drill("bad_secret", undo=True, run=k)
    assert _warehouse(k) == "postgresql://warehouse:real-pw-1@postgres.dip-data:5432/warehouse"
    assert drills.SAVED not in k.secret["metadata"]["annotations"]
    assert "nothing to restore" in drills.drill("bad_secret", undo=True, run=k)


def test_no_kubectl_argument_carries_a_password_in_clear():
    # the patch holds the values base64-encoded, as the Secret does
    k = SecretKubectl()
    drills.drill("bad_secret", run=k)
    bad_pw = _warehouse(k).split(":")[2].split("@")[0]
    assert not any("real-pw-1" in a or bad_pw in a for c in k.calls for a in c)
