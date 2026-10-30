"""Intake jobs: the queue the multi-agent intake team works from (Stage 10, S10.2; PLAN.md
section 8.12).

A job asks for one contract: `onboard` (a new vendor's sample, no contract yet) or `drift`
(a feed whose file no longer fits its contract, and whose vendor change a person agreed the
contract should take). Two things queue one:

  - a sample landed under `landing/_onboard/<pack>.<name>/<file>` in MinIO. The API and
    SFTP channels refuse a feed with no contract (no API key or SFTP account exists for
    it), so a new vendor's first file comes in this way. `scan_onboarding` (the agent_chain
    DAG's `intake_jobs` task, every 5 minutes) registers it as a sample (its raw copy under
    raw/samples/, as `dip contracts sample` does), removes it from landing, and queues an
    `onboard` job;
  - a person executes triage's `propose_contract_change` (dip/agents/approvals.py): the
    group's latest quarantined file becomes a `drift` job, where before it only returned a
    note telling the person to run `dip contracts author` themselves.

At most one job per feed is open (queued or running): a second sample, or a second
handoff, for a feed already being worked on is registered but not queued. Jobs are claimed
one at a time with the same rules as ops.agent_run (`for update skip locked`; a `running`
claim older than STALE_AFTER, a dead pod's, is taken back). The intake graph that runs a
claimed job (profile, map, validate, review, propose) is S10.4; until then nothing claims.

Nothing here writes a contract, a model or a branch: a job ends, at most, in a proposal a
person approves.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from dip import BUCKET_LANDING

ONBOARD_PREFIX = "_onboard/"
FEED = re.compile(r"^(?P<pack>[a-z][a-z0-9_]*)\.(?P<name>[a-z][a-z0-9_]*)$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
STALE_AFTER = timedelta(minutes=30)
OPEN = ("queued", "running")
ENDS = ("proposed", "no_change", "revise", "rejected", "failed", "skipped")


def queue(cur: Any, feed: str, mode: str, file_sha256: str, source: str,
          source_ref: str | None = None) -> int | None:  # fmt: skip
    """A queued job's id, or None when the feed already has an open job. Uncommitted."""
    cur.execute(
        "insert into ops.intake_job (feed, mode, file_sha256, source, source_ref)"
        " values (%s, %s, %s, %s, %s)"
        " on conflict (feed) where status in ('queued', 'running') do nothing"
        " returning job_id",
        (feed, mode, file_sha256, source, source_ref),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def onboard_target(key: str, contracts: dict[str, Any]) -> tuple[str, str] | str:
    """(feed, file name) for a landing key under _onboard/, or why it is not taken. The
    pack must already exist (have a contract), and the feed must not."""
    parts = key.split("/")
    if len(parts) != 3 or parts[0] + "/" != ONBOARD_PREFIX:
        return f"{key}: expected {ONBOARD_PREFIX}<pack>.<name>/<file>"
    feed, name = parts[1], parts[2]
    m = FEED.match(feed)
    if not m:
        return f"{key}: the folder must be <pack>.<name>, lower case"
    if not NAME.match(name):
        return f"{key}: unusable file name"
    if feed in contracts:
        return f"{key}: {feed} already has a contract (send its files to landing/{feed}/)"
    if not any(f.split(".")[0] == m["pack"] for f in contracts):
        return f"{key}: no pack named {m['pack']!r}"
    return feed, name


def scan_onboarding(conn: Any, store: Any, contracts: dict[str, Any],
                    by: str = "intake:landing") -> dict[str, list[str]]:  # fmt: skip
    """Register every sample under landing/_onboard/ and queue its onboarding job. A key
    that is not usable is left where it is and reported (nothing is queued for it)."""
    from dip.agents.author_tools import register_sample_bytes

    out: dict[str, list[str]] = {"queued": [], "already_open": [], "refused": []}
    for key in sorted(store.list(BUCKET_LANDING, ONBOARD_PREFIX)):
        target = onboard_target(key, contracts)
        if isinstance(target, str):
            out["refused"].append(target)
            continue
        feed, name = target
        sha = register_sample_bytes(conn, store, feed, name, store.get(BUCKET_LANDING, key), by)
        with conn.cursor() as cur:
            job = queue(cur, feed, "onboard", sha, "landing", key)
        conn.commit()
        store.delete(BUCKET_LANDING, key)  # registered: its copy is under raw/samples/
        (out["queued"] if job else out["already_open"]).append(f"{feed} {sha[:12]}")
    return out


def drift_from_handoff(cur: Any, feed: str, quarantine_ids: list[int],
                       approval_id: int) -> dict[str, Any]:  # fmt: skip
    """An executed propose_contract_change: the group's latest quarantined file (a file
    quarantined whole first) becomes a drift job. Uncommitted: the approval's execution
    commits it with the approval's status."""
    cur.execute(
        "select file_sha256 from ops.quarantine where quarantine_id = any(%s) and feed = %s"
        " order by (level = 'file') desc, created_at desc, quarantine_id desc limit 1",
        (quarantine_ids, feed),
    )
    row = cur.fetchone()
    if row is None:
        return {"handoff": "none: the group has no open entries", "job_id": None}
    job = queue(cur, feed, "drift", row[0], "handoff", f"approval:{approval_id}")
    return {
        "handoff": "intake team (drift)" if job else f"{feed} already has an open intake job",
        "job_id": job,
        "file_sha256": row[0],
    }


def claim_one(conn: Any, dag_run_id: str | None = None) -> dict[str, Any] | None:
    """The oldest queued job, or a dead pod's stale `running` one, claimed as `running`."""
    with conn.cursor() as cur:
        cur.execute(
            "update ops.intake_job set status = 'running', claimed_at = now(),"
            " dag_run_id = %s where job_id = (select job_id from ops.intake_job"
            "   where status = 'queued' or (status = 'running' and claimed_at < now() - %s)"
            "   order by created_at, job_id limit 1 for update skip locked)"
            " returning job_id, feed, mode, file_sha256",
            (dag_run_id, STALE_AFTER),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        return None
    return dict(zip(("job_id", "feed", "mode", "file_sha256"), row, strict=True))


def finish(conn: Any, job_id: int, status: str, reason: str | None = None,
           chain_id: str | None = None, provider: str | None = None,
           approval_id: int | None = None) -> None:  # fmt: skip
    if status not in ENDS:
        raise ValueError(f"a job ends as one of {ENDS}, not {status!r}")
    with conn.cursor() as cur:
        cur.execute(
            "update ops.intake_job set status = %s, reason = %s, chain_id = %s,"
            " provider = %s, approval_id = %s, finished_at = now() where job_id = %s",
            (status, reason and reason[:2000], chain_id, provider, approval_id, job_id),
        )
    conn.commit()
