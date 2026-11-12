"""Stage 14 (14.2): the restore drill. `dip drill drop_table` snapshots and drops the table
on kind-dip only; `dip restore --latest --verify` checks the dump's hash before it touches
the pod, restores into warehouse_restore (never the live database), brings back only the
dropped table's entries, and compares count, checksum, owner and grants with the snapshot.
The SQL itself runs against Postgres in tests/integration/test_restore.py."""

from __future__ import annotations

import hashlib

import pytest

from dip import drills, restore
from dip.drills import DrillError

DUMP = b"PGDMP fake custom archive"
SIZE = len(DUMP)
SHA = hashlib.sha256(DUMP).hexdigest()
KEY = "warehouse/warehouse-20261109T021503Z.dump"
LISTING = """\
;
; Archive created at 2026-11-09 02:15:04 UTC
;     dbname: warehouse
3001; 2615 16390 SCHEMA - gold warehouse
3101; 1259 16500 TABLE gold member_eligibility warehouse
3102; 1259 16510 TABLE gold member_eligibility_old warehouse
3103; 1259 16520 TABLE silver member_eligibility warehouse
3201; 0 16500 TABLE DATA gold member_eligibility warehouse
3202; 0 16510 TABLE DATA gold member_eligibility_old warehouse
3301; 0 0 ACL gold TABLE member_eligibility warehouse
3302; 0 0 COMMENT gold COLUMN member_eligibility.member_id warehouse
3303; 0 0 ACL gold TABLE member_eligibility_old warehouse
3304; 0 0 ACL - SCHEMA gold postgres
"""


URL = f"http://minio.dip-data.svc.cluster.local:9000/backups/{KEY}?X-Amz-Signature=s"


class S3:
    """Signs a GET URL; makes no request."""

    def __init__(self):
        self.calls = []

    def generate_presigned_url(self, method, Params, ExpiresIn):  # noqa: N803 - boto3's names
        self.calls.append((method, Params, ExpiresIn))
        return URL


class Kube:
    """kubectl against a fake postgres-0: answers psql by what the SQL asks."""

    def __init__(self, context="kind-dip", pending=True, there=False, copy_sha=SHA,
                 copy_size=SIZE, live=("42", "abc"),
                 acl="{warehouse=arwdDxtm/warehouse}"):  # fmt: skip
        self.context, self.pending, self.there, self.copy_sha = context, pending, there, copy_sha
        self.copy_size = copy_size
        self.live, self.acl = live, acl
        self.calls: list[tuple] = []
        self.stdin: dict[str, bytes] = {}
        self.sql: list[tuple[str, str]] = []
        self.list_lines: list[str] = []
        self.lost_list = False  # the pod's list file ends up empty (a lost write)
        self.restored = False

    def __call__(self, *args, check=True, stdin=None, timeout=900):
        self.calls.append(args)
        if args[:2] == ("config", "current-context"):
            return self.context
        cmd = list(args[args.index("--") + 1 :])
        if stdin is not None:
            self.stdin[cmd[-1]] = stdin
        if cmd[0] == "sh" and cmd[2].startswith("printf") and restore.POD_LIST in cmd[2]:
            self.list_lines = [] if self.lost_list else cmd[4:]
            return ""
        if cmd[0] == "sh" and "grep -c" in cmd[-1]:
            return str(len(self.list_lines))
        if cmd[0] == "pg_restore" and "-L" in cmd:
            self.restored = True
            return ""
        if cmd[0] == "sh" and "sha256sum" in cmd[-1]:
            return f"   {self.copy_size}\n{self.copy_sha}  {restore.POD_DUMP}"
        if cmd[:2] == ["pg_restore", "--list"]:
            return LISTING
        if cmd[0] != "psql":
            return ""
        db, sql = cmd[cmd.index("-d") + 1], cmd[-1]
        self.sql.append((db, sql))
        if sql.startswith("select backup_id, object_key"):
            return f"7|{KEY}|{len(DUMP)}|{SHA}"
        if "from ops.restore_drill where restored_at is null" in sql and "drill_id, table" in sql:
            return ("3|gold.member_eligibility|42|abc|warehouse|{warehouse=arwdDxtm/warehouse}"
                    if self.pending else "")  # fmt: skip
        if "datdba" in sql:
            return "warehouse"
        if "to_regclass" in sql:
            return "t" if self.there or self.restored else "f"
        if "md5(string_agg" in sql:
            return "|".join(("42", "abc") if db == restore.RESTORE_DB else self.live)
        if "relacl" in sql:
            return f"warehouse|{self.acl}"
        return ""

    def execs(self) -> list[list[str]]:
        return [list(c[c.index("--") + 1 :]) for c in self.calls if "--" in c]


def run_restore(k: Kube, s3: S3 | None = None, verify: bool = True) -> dict:
    return restore.restore_latest(verify, run=k, s3=s3 or S3(), say=lambda _: None)


def test_restore_refuses_any_cluster_but_kind():
    k, s3 = Kube(context="prod-eu"), S3()
    with pytest.raises(DrillError, match="not the kind cluster"):
        run_restore(k, s3)
    assert k.calls == [("config", "current-context")] and s3.calls == []


def test_restore_brings_the_table_back_and_verifies_it():
    k = Kube()
    out = run_restore(k)
    assert out["verified"] is True and out["failed"] == [] and out["rows"] == 42
    execs = k.execs()
    # the pod fetches the dump with a signed URL, read from stdin (never an argument), and
    # checks it before anything is restored
    fetch = [c for c in execs if c[0] == "sh" and "wget" in c[-1]]
    assert len(fetch) == 1 and k.stdin[fetch[0][-1]] == (URL + "\n").encode()
    assert not any(URL in a for c in k.calls for a in c)
    check = [c for c in execs if c[0] == "sh" and "sha256sum" in c[-1]]
    dropdb = ["dropdb", "-U", "postgres", "--if-exists", "warehouse_restore"]
    assert execs.index(fetch[0]) < execs.index(check[0]) < execs.index(dropdb)
    assert ["dropdb", "-U", "postgres", "--if-exists", "warehouse_restore"] in execs
    assert ["createdb", "-U", "postgres", "-O", "warehouse", "warehouse_restore"] in execs
    full = ["pg_restore", "-U", "postgres", "-d", "warehouse_restore", "--exit-on-error",
            restore.POD_DUMP]  # fmt: skip
    back = ["pg_restore", "-U", "postgres", "-d", "warehouse", "--exit-on-error",
            "--single-transaction", "-L", restore.POD_LIST, restore.POD_DUMP]  # fmt: skip
    assert execs.index(full) < execs.index(back)
    # never a drop or a full restore of the live database
    assert not any(c[0] == "dropdb" and "warehouse" in c[-1:] for c in execs)
    assert [c for c in execs if c[0] == "pg_restore" and "warehouse" in c and "-L" not in c] == []
    # only the dropped table's own entries go back
    assert k.list_lines == [
        "3101; 1259 16500 TABLE gold member_eligibility warehouse",
        "3201; 0 16500 TABLE DATA gold member_eligibility warehouse",
        "3301; 0 0 ACL gold TABLE member_eligibility warehouse",
        "3302; 0 0 COMMENT gold COLUMN member_eligibility.member_id warehouse",
    ]
    update = [s for _, s in k.sql if s.startswith("update ops.restore_drill")]
    assert len(update) == 1 and "verified = true" in update[0] and "drill_id = 3" in update[0]
    assert "restore_backup_id = 7" in update[0]
    assert execs[-1] == ["rm", "-f", restore.POD_DUMP, restore.POD_LIST]


@pytest.mark.parametrize("bad", [{"copy_sha": "0" * 64}, {"copy_size": len(DUMP) - 1}])
def test_a_dump_that_does_not_match_its_row_is_never_restored(bad):
    k = Kube(**bad)
    with pytest.raises(DrillError, match="does not match ops.backup 7"):
        run_restore(k)
    assert not any(c[0] in ("pg_restore", "dropdb", "createdb") for c in k.execs())
    assert k.execs()[-1][:2] == ["rm", "-f"]  # the file is removed anyway


def test_a_restored_table_that_differs_is_recorded_as_not_verified():
    k = Kube(live=("41", "def"), acl="")
    out = run_restore(k)
    assert out["verified"] is False
    assert out["failed"] == ["rows in warehouse", "checksum in warehouse", "grants"]
    assert restore.main(True, run=Kube(live=("41", "def")), s3=S3(), say=lambda _: None) == 1
    update = [s for _, s in k.sql if s.startswith("update ops.restore_drill")][0]
    assert "verified = false" in update


def test_a_table_already_there_is_not_restored_over():
    k = Kube(there=True)
    assert run_restore(k)["verified"] is True
    assert not any("-L" in c for c in k.execs())


def test_verify_needs_a_drill_snapshot_and_restore_alone_does_not():
    with pytest.raises(DrillError, match="nothing to verify against"):
        run_restore(Kube(pending=False))
    k = Kube(pending=False)
    out = run_restore(k, verify=False)
    assert out["verified"] is None
    assert any(c[:3] == ["pg_restore", "-U", "postgres"] for c in k.execs())
    assert not any("-L" in c for c in k.execs())


def test_toc_entries_need_the_table_and_its_data():
    with pytest.raises(DrillError, match="no table and data entries"):
        restore.toc_entries("3101; 1259 16500 TABLE gold member_eligibility warehouse",
                            "gold.member_eligibility")  # fmt: skip


def test_table_names_are_checked_before_they_reach_sql():
    for bad in ("gold.x; drop table y", "gold", "Gold.X", "gold.x y"):
        with pytest.raises(DrillError):
            restore.checksum_sql(bad)
    assert "from gold.member_eligibility t" in restore.checksum_sql("gold.member_eligibility")


class DrillKube:
    def __init__(self, state="t||7", context="kind-dip"):
        self.state, self.context, self.calls = state, context, []

    def __call__(self, *args, check=True):
        self.calls.append(args)
        if args[:2] == ("config", "current-context"):
            return self.context
        sql = args[-1]
        if "to_regclass" in sql and "begin;" not in sql:
            return self.state
        if "begin;" in sql:
            return "5|42|abc|7"
        return ""


def test_drop_table_snapshots_then_drops_in_one_transaction_on_kind_only():
    k = DrillKube(context="prod-eu")
    with pytest.raises(DrillError, match="not the kind cluster"):
        drills.drill("drop_table", run=k)
    assert len(k.calls) == 1
    k = DrillKube()
    msg = drills.drill("drop_table", run=k)
    assert "drill 5" in msg and "42 rows" in msg and "restore --latest --verify" in msg
    sql = k.calls[-1][-1]
    assert k.calls[-1][:6] == ("-n", "dip-data", "exec", "postgres-0", "--", "psql")
    assert sql.index("insert into ops.restore_drill") < sql.index("drop table")
    assert "begin;" in sql and sql.rstrip().endswith("commit;")
    assert "drop table gold.member_eligibility;" in sql and "cascade" not in sql.lower()


@pytest.mark.parametrize(("state", "why"), [
    ("t|4|7", "already on"),
    ("f||7", "nothing to drop"),
    ("t||", "no good backup"),
])  # fmt: skip
def test_drop_table_refuses(state, why):
    k = DrillKube(state)
    with pytest.raises(DrillError, match=why):
        drills.drill("drop_table", run=k)
    assert not any("begin;" in c[-1] for c in k.calls)


def test_a_pending_drill_restores_from_the_backup_it_recorded():
    # A backup taken after the drop no longer holds the table: the drill's own one is used.
    k = Kube()
    run_restore(k)
    asked = [sql for _, sql in k.sql if sql.startswith("select backup_id, object_key")]
    assert asked == [restore.DRILL_BACKUP_SQL]


def test_a_restore_without_a_drill_takes_the_newest_backup():
    k = Kube(pending=False)
    run_restore(k, verify=False)
    asked = [sql for _, sql in k.sql if sql.startswith("select backup_id, object_key")]
    assert asked == [restore.NEWEST_BACKUP_SQL]


def test_a_list_lost_on_its_way_into_the_pod_stops_the_restore():
    """The upgrade E2E's second drill, 2026-11-11: an empty list made `pg_restore -L`
    restore nothing and exit 0. Now the list's lines are counted in the pod first."""
    k = Kube()
    k.lost_list = True
    with pytest.raises(DrillError, match="restore list in the pod has 0 lines"):
        run_restore(k)
    assert not k.restored
