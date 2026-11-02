"""Stage 11 (S11.6): `dip drill` runs only against the kind cluster, and each drill has an
undo; the pods' terminations are read as the platform check and get_k8s_events read them."""

from __future__ import annotations

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
