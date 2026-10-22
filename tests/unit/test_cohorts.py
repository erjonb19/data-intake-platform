"""R7 cohort library: file rules, small-cell suppression and the audit chain (no database)."""

from __future__ import annotations

import pytest

from dip import cohorts


@pytest.mark.parametrize(
    ("persons", "shown"),
    [
        ([500, 120, 80, 40], [500, 120, 80, 40]),
        ([500, 120, 115, 40], [500, 120, "suppressed", 40]),  # a drop of 5 would reveal 5
        ([500, 9, 3], [500, "suppressed", "suppressed"]),
        ([500, 120, 0], [500, 120, 0]),  # zero discloses nobody
        ([500, 500, 495], [500, 500, "suppressed"]),
        ([8], ["suppressed"]),
        ([0, 0], [0, 0]),
    ],
)
def test_suppress(persons, shown):
    assert cohorts.suppress(persons) == shown


def test_a_hidden_step_cannot_be_recovered_from_its_neighbours():
    # 120 -> 115 is hidden; the next shown step is compared with 120, not with 115.
    shown = cohorts.suppress([500, 120, 115, 113])
    assert shown == [500, 120, "suppressed", "suppressed"]


def _write(tmp_path, name, version, sql):
    path = tmp_path / name / f"v{version}.sql"
    path.parent.mkdir(parents=True)
    path.write_text(sql, encoding="utf-8")
    return path


HEADER = "-- title: T\n-- question: Q?\n"


def test_load_reads_metadata_and_hashes_the_exact_file(tmp_path):
    c = cohorts.load(_write(tmp_path, "demo", 2, HEADER + "-- caveat: C\nselect 0, 'a', 1\n"))
    assert (c.name, c.version, c.title, c.question, c.caveats) == ("demo", 2, "T", "Q?", ("C",))
    assert c.sql == "select 0, 'a', 1"
    assert len(c.sha256) == 64


@pytest.mark.parametrize(
    ("sql", "error"),
    [
        ("select 1", "title"),
        (HEADER + "select 1; drop table x", "one statement"),
        (HEADER + "delete from omop_shareable.person", "must be a query"),
    ],
)
def test_load_refuses(tmp_path, sql, error):
    with pytest.raises(ValueError, match=error):
        cohorts.load(_write(tmp_path, "demo", 1, sql))


def test_a_file_cannot_pose_as_another_version(tmp_path):
    path = tmp_path / "demo" / "latest.sql"
    path.parent.mkdir()
    path.write_text(HEADER + "select 1", encoding="utf-8")
    with pytest.raises(ValueError, match="v<N>"):
        cohorts.load(path)


def test_library_takes_the_latest_version(tmp_path):
    _write(tmp_path, "demo", 1, HEADER + "select 1")
    (tmp_path / "demo" / "v2.sql").write_text(HEADER + "select 2", encoding="utf-8")
    assert cohorts.library(tmp_path)["demo"].version == 2


def test_the_shipped_library_loads():
    lib = cohorts.library()
    assert "t2d_hba1c_monitoring" in lib
    assert all(c.question and c.caveats for c in lib.values())


def test_audit_chain_detects_edits_and_removals(tmp_path):
    log = cohorts.AuditLog(tmp_path / "audit.jsonl")
    for i in range(3):
        log.append("ran", cohort=f"c{i}")
    assert cohorts.verify_audit(log.entries()) == []
    entries = log.entries()
    entries[1]["cohort"] = "edited"
    assert cohorts.verify_audit(entries) == ["entry 1: content changed"]
    del entries[1]
    assert "entry 1: does not follow entry 0" in cohorts.verify_audit(
        log.entries()[:1] + log.entries()[2:]
    )


@pytest.mark.parametrize(
    ("rows", "problem"),
    [
        ([(0, "a", 500), (1, "b", 120), (2, "c", 125)], "larger than the one before"),
        ([(0, "a", 500), (2, "b", 120)], "not 0, 1, 2"),
        ([(0, "a", 500), (1, "b", None)], "must be counts"),
        ([(0, "a", 500), (1, "b", 120), (2, "c", 120)], None),
    ],
)
def test_check_steps(rows, problem):
    got = cohorts.check_steps(rows)
    assert (got is None) if problem is None else (problem in got)


def test_comment_lines_only(tmp_path):
    c = cohorts.load(_write(tmp_path, "demo", 1, HEADER + "select 0, 'a--b', 1\n"))
    assert c.sql == "select 0, 'a--b', 1"  # "--" inside a string literal is kept
    with pytest.raises(ValueError, match="block comments"):
        cohorts.load(_write(tmp_path, "demo2", 1, HEADER + "select /* x */ 1"))


def test_appends_wait_for_the_lock_and_chain_in_order(tmp_path):
    import threading

    log = cohorts.AuditLog(tmp_path / "audit.jsonl")
    threads = [threading.Thread(target=log.append, args=("ran",), kwargs={"cohort": f"c{i}"})
               for i in range(8)]  # fmt: skip
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(log.entries()) == 8
    assert cohorts.verify_audit(log.entries()) == []
