"""Stage 12 (12.2, #103): the platform-drill E2E creates its check Job from the CronJob's
template without the CronJob as owner, so the CronJob's history limit cannot delete it
before the E2E reads its state."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("platform_drill",
                                              ROOT / "tests/e2e/platform_drill.py")  # fmt: skip
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)

# The shape `kubectl create job --from=cronjob/... --dry-run=client -o json` prints.
RENDERED = json.dumps({
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {
        "name": "x",
        "namespace": "dip-airflow",
        "annotations": {"cronjob.kubernetes.io/instantiate": "manual"},
        "ownerReferences": [{"apiVersion": "batch/v1", "kind": "CronJob",
                             "name": "platform-check", "uid": "u", "controller": True}],
    },
    "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 90, "template": {"spec": {}}},
})  # fmt: skip


def test_the_check_job_has_no_owner_and_keeps_the_cronjobs_spec():
    job = json.loads(drill.check_job_manifest("platform-check-e2e-oom-1-1", RENDERED))
    assert "ownerReferences" not in job["metadata"]
    assert job["metadata"]["name"] == "platform-check-e2e-oom-1-1"
    assert job["metadata"]["labels"]["dip-e2e"] == "platform-check"
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 90
    assert job["spec"]["ttlSecondsAfterFinished"] == 180


def test_run_check_uses_it():
    src = (ROOT / "tests/e2e/platform_drill.py").read_text("utf-8")
    body = src.split("def run_check", 1)[1].split("\ndef ", 1)[0]
    assert "create_check_job(name)" in body and "--from=cronjob" not in body
