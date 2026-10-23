"""R7: a governed library of longitudinal cohort and feasibility queries over the
de-identified OMOP tier (omop_shareable), Synthea only.

A cohort is a versioned SQL file in `packs/healthcare/cohorts/<name>/v<N>.sql`. Its header
comment carries the metadata (`-- title:`, `-- question:`, `-- caveat:` lines) and its body
is one read-only query returning the attrition table: `step` (0, 1, ...), `criterion`
(text) and `persons` (distinct persons still in the cohort after that step). Longitudinal
criteria (diagnosed before a date, two results within a year, an event after another) are
written in SQL over the tier's tables, where dates are shifted per person but every
interval within a person is exact.

`run()` is the only way a cohort reaches the data. It:
1. connects with the cohort reader role (dip_cohort_reader: read-only, timeouts, no access
   outside omop_shareable) and refuses to run unless `cdm_source` is exactly DIP-SYNTHEA.
   This mirrors the warehouse's own grant rule (dbt hook grant_cohort_reader), so neither
   side trusts the other;
2. checks the file is one statement and runs it in a read-only transaction;
3. suppresses small cells: a count from 1 to 10 is shown as "suppressed", and so is any
   later step whose drop from the previous shown step is 1 to 10 (subtracting would reveal
   it). Zero is shown: it discloses nobody. Every hidden cell has the same label;
4. appends the run (the cohort, the SHA-256 of its exact SQL, what was released, the
   tier's vocabulary version) to a hash-chained JSONL audit log that `verify_audit` checks;
   refusals and failures are audited too.

Differencing across cohorts (a later cohort whose count, subtracted from a released one,
gives a small group) and across rebuilds (step 0 before and after new patients arrive) is
not checked by code: the control is that every library file is reviewed before it merges,
against what the library already releases.

Counts on this tier are approximate at the edges: dates move by up to 180 days per person
and only the year of birth is kept, so an age or a cut-off date can shift (see
docs/research/cohort-service.md). The data is synthetic, but suppression stays on so the
library behaves as it would on real data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dip.contracts import REPO_ROOT

LIBRARY = REPO_ROOT / "packs" / "healthcare" / "cohorts"
MIN_CELL = 11
SUPPRESSED = "suppressed"
REQUIRED_SOURCE = "DIP-SYNTHEA"
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


class Refused(RuntimeError):
    """The run did not happen; the reason is audited too."""


@dataclass(frozen=True)
class Cohort:
    name: str
    version: int
    title: str
    question: str
    caveats: tuple[str, ...]
    sql: str
    sha256: str
    path: str


@dataclass
class Result:
    cohort: str
    version: int
    sha256: str
    source: str
    attrition: list[dict[str, Any]] = field(default_factory=list)
    audit_head: str = ""  # this run's audit entry hash: keep it elsewhere as an anchor


def load(path: Path) -> Cohort:
    """A cohort file. Its name and version must match its path, so a file cannot pose as
    another cohort or version."""
    name, stem = path.parent.name, path.stem
    if not _NAME.match(name) or not re.fullmatch(r"v[1-9][0-9]*", stem):
        raise ValueError(f"{path}: expected packs/healthcare/cohorts/<name>/v<N>.sql")
    sql = path.read_text(encoding="utf-8")
    meta: dict[str, list[str]] = {}
    for line in sql.splitlines():
        if m := re.match(r"^--\s*(title|question|caveat):\s*(.+)$", line):
            meta.setdefault(m.group(1), []).append(m.group(2).strip())
    if "title" not in meta or "question" not in meta:
        raise ValueError(f"{path}: needs '-- title:' and '-- question:' header lines")
    body = _strip_comments(sql).strip().rstrip(";").strip()
    if ";" in body:
        raise ValueError(f"{path}: one statement only")
    if not re.match(r"^(with|select)\b", body, re.IGNORECASE):
        raise ValueError(f"{path}: must be a query (WITH or SELECT)")
    return Cohort(
        name=name,
        version=int(stem[1:]),
        title=meta["title"][0],
        question=" ".join(meta["question"]),
        caveats=tuple(meta.get("caveat", [])),
        sql=body,
        sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        path=path.relative_to(REPO_ROOT).as_posix()
        if path.is_relative_to(REPO_ROOT)
        else str(path),
    )


def _strip_comments(sql: str) -> str:
    """Drop comment lines (a line whose first non-space characters are --). Comments after
    code on a line are not allowed in library files, so a "--" inside a string literal is
    never mistaken for one; block comments are not allowed either."""
    lines = [line for line in sql.splitlines() if not line.lstrip().startswith("--")]
    body = "\n".join(lines)
    if "/*" in body:
        raise ValueError("block comments are not allowed in library files")
    return body


def library(root: Path = LIBRARY) -> dict[str, Cohort]:
    """The latest version of every cohort in the library."""
    out: dict[str, Cohort] = {}
    for path in sorted(root.glob("*/v*.sql")):
        c = load(path)
        if c.name not in out or c.version > out[c.name].version:
            out[c.name] = c
    return out


def suppress(persons: list[int]) -> list[int | str]:
    """Attrition counts as released. A count of 1-10 is hidden; so is any step whose drop
    from the last released step is 1-10, so no small group can be recovered by subtraction.
    Hidden cells all carry the same label, which says nothing about their size."""
    shown: list[int | str] = []
    last: int | None = None  # the last count released as a number
    for n in persons:
        if 1 <= n < MIN_CELL or (last is not None and 1 <= last - n < MIN_CELL):
            shown.append(SUPPRESSED)
        else:
            shown.append(n)
            last = n
    return shown


class AuditLog:
    """Append-only JSONL; each entry carries the SHA-256 of the previous one.

    What the chain shows: an entry edited, removed from the middle, or moved breaks it. What
    it cannot show on its own: the last entries cut off, or the whole file rewritten by
    someone who can write it. Each run therefore prints its entry's hash (`head`), which a
    caller can keep elsewhere as an anchor. Appends take a lock file, so two concurrent runs
    never chain to the same entry."""

    def __init__(self, path: Path):
        self.path = path

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text("utf-8").splitlines() if line]

    @contextmanager
    def _locked(self, timeout: float = 30.0):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except (FileExistsError, PermissionError):  # Windows: held, or being removed
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{lock} is held; remove it if no run is active") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        with self._locked():
            prev = self.entries()
            entry = {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "event": event,
                **fields,
                "prev": prev[-1]["hash"] if prev else "",
            }
            entry["hash"] = hashlib.sha256(
                json.dumps(entry, sort_keys=True, default=str).encode()
            ).hexdigest()
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return entry


def verify_audit(entries: list[dict[str, Any]]) -> list[str]:
    """Problems with the chain: an edited, removed or reordered entry breaks it."""
    problems, prev = [], ""
    for i, e in enumerate(entries):
        body = {k: v for k, v in e.items() if k != "hash"}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        if e.get("prev") != prev:
            problems.append(f"entry {i}: does not follow entry {i - 1}")
        if digest != e.get("hash"):
            problems.append(f"entry {i}: content changed")
        prev = e.get("hash", "")
    return problems


def check_steps(rows: list[tuple]) -> str | None:
    """Why an attrition table cannot be released, or None. Steps must be 0, 1, 2, ..., every
    count a whole number, and never larger than the step before: a step that grows (a join
    that duplicates people) would release two counts whose small difference is a person
    count."""
    if [r[0] for r in rows] != list(range(len(rows))):
        return "attrition steps are not 0, 1, 2, ..."
    counts = [r[2] for r in rows]
    if any(not isinstance(n, int) or n < 0 for n in counts):
        return f"persons must be counts, got {counts}"
    if any(b > a for a, b in zip(counts, counts[1:], strict=False)):
        return f"a step is larger than the one before it: {counts}"
    return None


def run(cohort: Cohort, dsn: str, audit: AuditLog, requester: str) -> Result:
    """Run one library cohort as the cohort reader and return suppressed counts. Every
    outcome is audited: ran, refused, or failed."""
    who = {"cohort": cohort.name, "version": cohort.version, "sha256": cohort.sha256,
           "requester": requester}  # fmt: skip
    try:
        return _run(cohort, dsn, audit, who)
    except Refused:
        raise
    except Exception as exc:
        audit.append("failed", **who, error=f"{type(exc).__name__}: {exc}"[:500])
        raise


def _refuse(audit: AuditLog, who: dict, reason: str) -> Refused:
    audit.append("refused", **who, reason=reason)
    return Refused(reason)


def _run(cohort: Cohort, dsn: str, audit: AuditLog, who: dict) -> Result:
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        # One snapshot for the cdm_source check and the query, so they see the same tier.
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        with conn.cursor() as cur:
            cur.execute("select current_user, session_user")
            users = cur.fetchone()
            # Both: a privileged login that sets role=dip_cohort_reader could reset it.
            if users != ("dip_cohort_reader", "dip_cohort_reader"):
                raise _refuse(audit, who, f"connected as {users}, not the cohort reader")
            try:
                cur.execute(
                    "select cdm_source_abbreviation, vocabulary_version"
                    " from omop_shareable.cdm_source"
                )
                sources = cur.fetchall()
            except psycopg.Error as exc:
                raise _refuse(audit, who, f"no readable cdm_source: {exc}") from exc
            if [s for s, _ in sources] != [REQUIRED_SOURCE]:
                raise _refuse(audit, who, f"cdm_source is {sources}, not {REQUIRED_SOURCE}")
            cur.execute(cohort.sql)
            rows = sorted(cur.fetchall(), key=lambda r: r[0])
    if problem := check_steps(rows):
        raise _refuse(audit, who, f"{cohort.name}: {problem}")
    shown = suppress([r[2] for r in rows])
    result = Result(
        cohort.name, cohort.version, cohort.sha256, f"{REQUIRED_SOURCE} {sources[0][1]}",
        [{"step": r[0], "criterion": r[1], "persons": s} for r, s in zip(rows, shown, strict=True)],
    )  # fmt: skip
    entry = audit.append("ran", **who, source=result.source, released=result.attrition)
    result.audit_head = entry["hash"]
    return result


def as_dict(result: Result) -> dict[str, Any]:
    return asdict(result)
