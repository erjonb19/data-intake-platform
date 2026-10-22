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
   tier's vocabulary version) to a hash-chained JSONL audit log that `verify_audit` checks.

Counts on this tier are approximate at the edges: dates move by up to 180 days per person
and only the year of birth is kept, so an age or a cut-off date can shift (see
docs/research/cohort-service.md). The data is synthetic, but suppression stays on so the
library behaves as it would on real data.
"""

from __future__ import annotations

import hashlib
import json
import re
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
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


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
    """Append-only JSONL; each entry carries the SHA-256 of the previous one."""

    def __init__(self, path: Path):
        self.path = path

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text("utf-8").splitlines() if line]

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
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


def run(cohort: Cohort, dsn: str, audit: AuditLog, requester: str) -> Result:
    """Run one library cohort as the cohort reader and return suppressed counts."""
    import psycopg

    who = {"cohort": cohort.name, "version": cohort.version, "sha256": cohort.sha256,
           "requester": requester}  # fmt: skip
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("select current_user")
            (user,) = cur.fetchone()
            if user != "dip_cohort_reader":
                audit.append("refused", **who, reason=f"connected as {user}, not the reader")
                raise Refused(f"cohorts run only as dip_cohort_reader, not {user}")
            try:
                cur.execute(
                    "select cdm_source_abbreviation, vocabulary_version"
                    " from omop_shareable.cdm_source"
                )
                sources = cur.fetchall()
            except psycopg.Error as exc:
                audit.append("refused", **who, reason=f"no readable cdm_source: {exc}")
                raise Refused("the tier's cdm_source is not readable") from exc
            if [s for s, _ in sources] != [REQUIRED_SOURCE]:
                audit.append("refused", **who, reason=f"cdm_source is {sources}")
                raise Refused(f"cohorts run on {REQUIRED_SOURCE} only, not {sources}")
            cur.execute(cohort.sql)
            rows = cur.fetchall()
    rows.sort(key=lambda r: r[0])
    if [r[0] for r in rows] != list(range(len(rows))):
        audit.append("refused", **who, reason="attrition steps are not 0, 1, 2, ...")
        raise Refused(f"{cohort.name}: the query must return steps 0, 1, 2, ...")
    shown = suppress([int(r[2]) for r in rows])
    result = Result(
        cohort.name, cohort.version, cohort.sha256, f"{REQUIRED_SOURCE} {sources[0][1]}",
        [{"step": r[0], "criterion": r[1], "persons": s} for r, s in zip(rows, shown, strict=True)],
    )  # fmt: skip
    audit.append("ran", **who, source=result.source, released=result.attrition)
    return result


def as_dict(result: Result) -> dict[str, Any]:
    return asdict(result)
