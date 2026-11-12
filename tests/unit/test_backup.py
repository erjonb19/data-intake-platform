"""Stage 14 (14.1): the warehouse backup's retention, its ops.backup row on every outcome,
and `dip backup now` (a Job from the CronJob, without its owner, on kind-dip only)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dip import backup
from dip.drills import DrillError

T0 = datetime(2026, 11, 9, 2, 15, 3, tzinfo=UTC)


def key(day: int) -> str:
    return f"warehouse/warehouse-202611{day:02d}T021500Z.dump"


def test_dump_keys_are_utc_and_sort_by_time():
    assert backup.dump_key(T0) == "warehouse/warehouse-20261109T021503Z.dump"
    assert backup.DUMP_KEY.match(backup.dump_key(T0))


def test_retention_keeps_the_newest_seven_and_deletes_the_rest_oldest_first():
    keys = [key(d) for d in (5, 1, 9, 3, 2, 8, 4, 7, 6)]  # any order
    assert backup.to_prune(keys) == [key(1), key(2)]
    assert backup.to_prune(keys, keep=8) == [key(1)]
    assert backup.to_prune(keys[:7]) == []
    assert backup.to_prune([]) == []


def test_retention_never_touches_other_objects():
    others = ["warehouse/notes.txt", "warehouse/warehouse-latest.dump",
              "other/warehouse-20260101T000000Z.dump"]  # fmt: skip
    keys = [key(d) for d in range(1, 10)] + others
    assert backup.to_prune(keys, keep=1) == [key(d) for d in range(1, 9)]


def test_retention_keeps_at_least_one():
    with pytest.raises(ValueError):
        backup.to_prune([key(1)], keep=0)


class FakeConn:
    def __init__(self):
        self.rows: dict[int, dict] = {}

    def cursor(self):
        conn = self

        class Cur:
            def execute(self, sql, params):
                if sql.startswith("insert"):
                    self.id = len(conn.rows) + 1
                    conn.rows[self.id] = {"started_at": params[0], "status": "running"}
                else:
                    assert sql.startswith("update ops.backup set")
                    finished, status, error, key_, size, sha, bid = params
                    conn.rows[bid].update(finished_at=finished, status=status, error=error,
                                          object_key=key_, bytes=size, sha256=sha)  # fmt: skip

            def fetchone(self):
                return (self.id,)

        return Cur()

    def commit(self):
        pass


class FakeStore:
    def __init__(self, keys=(), fail_upload=False, fail_listing=0):
        self.objects: dict[str, bytes] = {k: b"x" for k in keys}
        self.fail_upload, self.fail_listing, self.listings = fail_upload, fail_listing, 0
        self.metadata: dict[str, dict] = {}

    def upload(self, path: Path, key: str, metadata: dict[str, str]) -> None:
        if self.fail_upload:
            raise ConnectionError("minio unreachable")
        self.objects[key] = path.read_bytes()
        self.metadata[key] = metadata

    def sizes(self) -> dict[str, int]:
        self.listings += 1
        if self.listings == self.fail_listing:
            raise ConnectionError("list failed")
        return {k: len(v) for k, v in self.objects.items()}

    def delete(self, key: str) -> None:
        del self.objects[key]


def work(tmp_path: Path, status="ok", dump=b"PGDMP-archive", error="") -> Path:
    (tmp_path / "started_at").write_text("2026-11-09T02:15:03Z\n")
    (tmp_path / "status").write_text(status + "\n")
    (tmp_path / "error").write_text(error)
    if dump is not None:
        (tmp_path / "warehouse.dump").write_bytes(dump)
    return tmp_path


def test_a_good_dump_is_uploaded_recorded_and_pruned(tmp_path):
    conn, store = FakeConn(), FakeStore([key(d) for d in range(1, 8)])
    out = backup.run(conn, store, work(tmp_path), now=lambda: T0)
    want = "warehouse/warehouse-20261109T021503Z.dump"
    assert out["status"] == "ok" and out["object_key"] == want and out["error"] is None
    assert out["pruned"] == [key(1)]  # 7 old + 1 new: the oldest goes
    assert sorted(store.objects) == [key(d) for d in range(2, 8)] + [want]
    sha = hashlib.sha256(b"PGDMP-archive").hexdigest()
    assert store.metadata[want] == {"sha256": sha}
    assert conn.rows[1] == {"started_at": datetime(2026, 11, 9, 2, 15, 3, tzinfo=UTC),
                            "status": "ok", "finished_at": T0, "error": None,
                            "object_key": want, "bytes": 13, "sha256": sha}  # fmt: skip


def test_a_failed_dump_is_recorded_with_its_error_and_nothing_is_uploaded(tmp_path):
    conn, store = FakeConn(), FakeStore([key(1)])
    err = "pg_dump: error: connection to server failed: FATAL: password authentication failed"
    out = backup.run(conn, store, work(tmp_path, "failed", None, err), now=lambda: T0)
    assert out["status"] == "failed" and conn.rows[1]["status"] == "failed"
    assert conn.rows[1]["error"] == "pg_dump: " + err and conn.rows[1]["object_key"] is None
    assert list(store.objects) == [key(1)]  # retention never runs after a failure


def test_an_empty_dump_and_a_missing_status_fail(tmp_path):
    out = backup.run(FakeConn(), FakeStore(), work(tmp_path, dump=b""), now=lambda: T0)
    assert out["status"] == "failed" and "empty" in out["error"]
    (tmp_path / "status").unlink()
    out = backup.run(FakeConn(), FakeStore(), tmp_path, now=lambda: T0)
    assert out["status"] == "failed" and out["error"].startswith("pg_dump: ")


def test_an_upload_failure_is_recorded(tmp_path):
    conn = FakeConn()
    out = backup.run(conn, FakeStore(fail_upload=True), work(tmp_path), now=lambda: T0)
    assert out["status"] == "failed"
    assert conn.rows[1]["error"] == "upload: ConnectionError: minio unreachable"


def test_a_retention_failure_keeps_the_good_dump_but_says_so(tmp_path):
    # the first listing checks the upload's size; the second, for retention, fails
    conn, store = FakeConn(), FakeStore(fail_listing=2)
    out = backup.run(conn, store, work(tmp_path), now=lambda: T0)
    assert out["status"] == "ok" and conn.rows[1]["object_key"]
    assert conn.rows[1]["error"] == "retention: ConnectionError: list failed"


def test_the_error_kept_is_the_tail(tmp_path):
    conn = FakeConn()
    backup.run(conn, FakeStore(), work(tmp_path, "failed", None, "x" * 5000), now=lambda: T0)
    assert len(conn.rows[1]["error"]) == 1000


# The shape `kubectl create job --from=cronjob/warehouse-backup --dry-run=client -o json`.
RENDERED = json.dumps({
    "apiVersion": "batch/v1", "kind": "Job",
    "metadata": {"name": "x", "namespace": "dip-data",
                 "annotations": {"cronjob.kubernetes.io/instantiate": "manual"},
                 "ownerReferences": [{"apiVersion": "batch/v1", "kind": "CronJob",
                                      "name": "warehouse-backup", "uid": "u"}]},
    "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 1800, "template": {"spec": {}}},
})  # fmt: skip


def test_the_now_job_has_no_owner_a_ttl_and_the_cronjobs_spec():
    job = json.loads(backup.job_manifest("warehouse-backup-now-1", RENDERED))
    assert "ownerReferences" not in job["metadata"]
    assert job["metadata"]["name"] == "warehouse-backup-now-1"
    assert job["metadata"]["labels"]["dip-backup"] == "manual"
    assert job["spec"]["ttlSecondsAfterFinished"] == 900
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 1800


def fake_kubectl(context="kind-dip", states=("/", "1/")):
    calls, states = [], list(states)

    def run(*args, stdin=None, check=True):
        calls.append((args, stdin))
        if args[:2] == ("config", "current-context"):
            return context
        if "--dry-run=client" in args:
            return RENDERED
        if args[2:4] == ("get", "job"):
            return states.pop(0)
        return ""

    return run, calls


def test_backup_now_creates_the_job_from_the_cronjob_and_waits_for_it():
    run, calls = fake_kubectl()
    name, state = backup.now_job(run, sleep=lambda s: None)
    assert state == "ok" and name.startswith("warehouse-backup-now-")
    dry = next(a for a, _ in calls if "--dry-run=client" in a)
    assert "--from=cronjob/warehouse-backup" in dry and dry[:2] == ("-n", "dip-data")
    created = next(s for a, s in calls if a[2:5] == ("create", "-f", "-"))
    assert "ownerReferences" not in json.loads(created)["metadata"]


def test_a_failed_job_is_reported():
    run, _ = fake_kubectl(states=("/1",))
    assert backup.now_job(run, sleep=lambda s: None)[1] == "failed"


def test_backup_now_refuses_any_cluster_but_kind_dip():
    run, calls = fake_kubectl(context="prod-cluster")
    with pytest.raises(DrillError, match="kind-dip"):
        backup.now_job(run, sleep=lambda s: None)
    assert len(calls) == 1  # nothing created


def test_the_dump_leaves_out_the_deid_keys_rows():
    """24.12: the backup never carries ops_secret.deid_key's rows into the bucket."""
    from pathlib import Path

    tf = (Path(__file__).resolve().parents[2] / "infra/terraform/backups.tf").read_text("utf-8")
    dump = tf[tf.index("if pg_dump") : tf.index("--file=/backup/warehouse.dump")]
    assert "--exclude-table-data='ops_secret.*'" in dump
