"""The feedback loop (Stage 11, S11.5; PLAN.md section 8.13): what people decided about each
agent's proposals, and a rejected proposal turned into a new eval case.

    python -m dip agents feedback                  approved, rejected and expired per agent,
                                                   action and week (ops.agent_feedback)
    python -m dip evals from-rejection APPROVAL_ID [--out DIR]

`from-rejection` writes a candidate case for the agent that made the proposal, under
`agents/evals/<agent>/candidates/` (by default), for a person to edit, record (a live call:
`--record` with the matching eval command) and commit. A triage proposal becomes a triage
case: the quarantined files of the proposal's group, the gold for that group, and what was
rejected and why. A contract proposal becomes a contract-author case over its sample file.

**Every value in the case is scrubbed to a synthetic one before it is written** (approved
2026-10-31): each cell keeps its shape (letters, digits, punctuation) with new characters,
and stays on the same side of every contract rule its column has, so the case quarantines
for the same reasons as the original: a value matching the column's `pattern` still
matches it (one that broke it still breaks it), a date within `min`..`max` stays within
(one outside stays outside, on the same side), and a value from the `allowed` list stays
(it is the contract's, not the vendor's; one not allowed is replaced by another not
allowed). Column names and file names are kept: they are the
schema the case tests. The command refuses a proposal whose inputs are not from the
platform's synthetic packs (a MIMIC-backed run, or any feed outside those packs), and a
format it cannot scrub cell by cell (only CSV).
"""

from __future__ import annotations

import csv
import io
import random
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from dip import BUCKET_RAW
from dip.contracts import REPO_ROOT, Contract

# The packs whose every file is generated (Synthea-derived healthcare, invented finance).
# packs/mimic is real, if de-identified, data: never.
SYNTHETIC_PACKS = ("healthcare", "finance")
CASES_DIR = REPO_ROOT / "agents" / "evals"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NEWLINE = chr(10)


class FeedbackError(Exception):
    """The proposal cannot be turned into a case (and why)."""


def feedback(conn: Any) -> list[dict[str, Any]]:
    cols = ("agent", "action", "week", "approved", "rejected", "expired", "wrong_root_cause",
            "wrong_action", "incomplete", "unsafe", "other_reason")  # fmt: skip
    with conn.cursor() as cur:
        cur.execute(f"select {', '.join(cols)} from ops.agent_feedback"
                    " order by agent, action, week")  # fmt: skip
        out = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    conn.commit()
    return out


# --- Scrubbing --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """What a column's contract says about its values, for keeping a scrubbed value on the
    same side of each rule."""

    allowed: frozenset[str] = frozenset()
    pattern: re.Pattern | None = None
    min: date | None = None
    max: date | None = None

    @classmethod
    def of(cls, col: dict[str, Any]) -> Rule:
        def day(v: Any) -> date | None:
            if v == "today":
                return date.today()
            try:
                return date.fromisoformat(str(v)) if v else None
            except ValueError:
                return None

        p = col.get("pattern")
        allowed = frozenset(str(v) for v in col.get("allowed") or [])
        return cls(allowed, re.compile(p) if p else None, day(col.get("min")), day(col.get("max")))


class Scrubber:
    """Shape-keeping replacements, the same for the same value within one case (so a key
    that repeats still repeats), seeded so a case is reproducible."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.seen: dict[str, str] = {}

    def _chars(self, value: str) -> str:
        out = []
        for ch in value:
            if "A" <= ch <= "Z":
                out.append(self.rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
            elif "a" <= ch <= "z":
                out.append(self.rng.choice("abcdefghijklmnopqrstuvwxyz"))
            elif "0" <= ch <= "9":
                out.append(self.rng.choice("0123456789"))
            elif ch.isalnum():  # any other script: a Latin letter of the same case
                out.append(self.rng.choice("abcdefghijklmnopqrstuvwxyz"))
            else:
                out.append(ch)
        return "".join(out)

    def _date(self, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError:  # an invalid date stays invalid in shape: keep its fault
            return self._chars(value)
        return (date(1950, 1, 1) + timedelta(days=self.rng.randrange(365 * 70))).isoformat()

    def _digits(self, value: str) -> str:
        return "".join(self.rng.choice("0123456789") if c.isdigit() else c for c in value)

    def _bounded_date(self, value: str, rule: Rule) -> str:
        """A random date on the same side of the column's min and max as the original."""
        d = date.fromisoformat(value)
        lo, hi = rule.min or date(1950, 1, 1), rule.max or date.today()
        if d < lo:
            lo, hi = lo - timedelta(days=3650), lo - timedelta(days=1)
        elif d > hi:
            lo, hi = hi + timedelta(days=1), hi + timedelta(days=3650)
        return (lo + timedelta(days=self.rng.randrange(max((hi - lo).days, 0) + 1))).isoformat()

    def _candidate(self, value: str, rule: Rule) -> str:
        if DATE.match(value):
            try:
                return self._bounded_date(value, rule)
            except ValueError:  # an invalid date stays invalid in shape: keep its fault
                return self._chars(value)
        if rule.pattern is not None and rule.pattern.fullmatch(value):
            return self._digits(value)  # an identifier's letters are its format, not data
        return self._chars(value)

    def value(self, value: str, rule: Rule | None = None) -> str:
        rule = rule or Rule()
        if not value or value in rule.allowed or not any(ch.isalnum() for ch in value):
            return value
        if value in self.seen:
            return self.seen[value]
        matched = rule.pattern.fullmatch(value) is not None if rule.pattern else None
        new = value
        for i in range(200):  # never the original, the contract's rules held as they were
            new = self._candidate(value, rule) if i < 100 else self._chars(value)
            if (
                new != value
                and new not in rule.allowed
                and (matched is None or (rule.pattern.fullmatch(new) is not None) == matched)
            ):
                break
        self.seen[value] = new
        return new


def scrub_csv(text: str, contract: Contract | None, seed: int, delimiter: str = ",") -> str:
    """Every cell after the header row scrubbed; the header (the schema) kept."""
    columns = contract.spec.get("columns", []) if contract else []
    rules = {c["name"]: Rule.of(c) for c in columns}
    s = Scrubber(seed)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        return text
    header, out = rows[0], [rows[0]]
    for row in rows[1:]:
        out.append([s.value(v, rules.get(header[i] if i < len(header) else ""))
                    for i, v in enumerate(row)])  # fmt: skip
    buf = io.StringIO()
    csv.writer(buf, delimiter=delimiter, lineterminator="\n").writerows(out)
    return buf.getvalue()


# --- From a rejection -------------------------------------------------------------------


class _Literal(yaml.SafeDumper):
    """A file's text as a `|` block, as the hand-written cases have it."""


_Literal.add_representer(str, lambda d, s: d.represent_scalar(
    "tag:yaml.org,2002:str", s, style="|" if NEWLINE in s else None))  # fmt: skip


def _approval(conn: Any, approval_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("select approval_id, agent_id, action, payload, status, decision_reason,"
                    " decision_note, run_id from ops.approvals where approval_id = %s",
                    (approval_id,))  # fmt: skip
        row = cur.fetchone()
    conn.commit()
    if row is None:
        raise FeedbackError(f"no approval {approval_id}")
    keys = ("approval_id", "agent_id", "action", "payload", "status", "reason", "note", "run_id")
    a = dict(zip(keys, row, strict=True))
    if a["status"] != "rejected":
        raise FeedbackError(f"approval {approval_id} is {a['status']}, not rejected")
    return a


def _synthetic(feed: str, contracts: dict[str, Contract]) -> Contract:
    pack = feed.partition(".")[0]
    if pack not in SYNTHETIC_PACKS:
        raise FeedbackError(f"{feed}: not from a synthetic pack ({', '.join(SYNTHETIC_PACKS)});"
                            " a case is made only from synthetic inputs")  # fmt: skip
    c = contracts.get(feed)
    if c is None:
        raise FeedbackError(f"{feed}: no contract")
    if str(c.path).replace("\\", "/").find("/packs/mimic/") >= 0:
        raise FeedbackError(f"{feed}: a MIMIC contract")
    return c


def _raw_files(conn: Any, store: Any, feed: str, shas: list[str]) -> list[tuple[str, bytes]]:
    with conn.cursor() as cur:
        cur.execute("select file_sha256, raw_key, source_key from ops.intake_file"
                    " where feed = %s and file_sha256 = any(%s) order by received_at",
                    (feed, shas))  # fmt: skip
        rows = cur.fetchall()
    conn.commit()
    # the file's name as it landed (the last part of its landing key)
    return [(source_key.rsplit("/", 1)[-1], store.get(BUCKET_RAW, raw_key))
            for _, raw_key, source_key in rows]  # fmt: skip


def _csv_text(c: Contract, data: bytes) -> str:
    fmt = c.spec.get("format", {})
    if fmt.get("type") != "csv":
        raise FeedbackError(f"{c.feed} is {fmt.get('type')}: only CSV files are scrubbed cell by"
                            " cell, so write this case by hand")  # fmt: skip
    return data.decode(fmt.get("encoding", "utf-8"), "replace")


def from_rejection(conn: Any, store: Any, contracts: dict[str, Contract], approval_id: int,
                   out_dir: Path | None = None) -> Path:  # fmt: skip
    """Write the candidate case and return its path."""
    a = _approval(conn, approval_id)
    p = a["payload"] or {}
    seed = approval_id
    origin = {"approval_id": approval_id, "rejected_action": a["action"],
              "reason": a["reason"], "note": (a["note"] or "")[:500],
              "agent_version": p.get("agent_version")}  # fmt: skip
    if a["action"] == "contract_pr":
        agent, case = "contract_author", _author_case(conn, store, contracts, a, p, seed)
    elif a["agent_id"] == "triage":
        agent, case = "triage", _triage_case(conn, store, contracts, a, p, seed)
    else:
        raise FeedbackError(f"no eval case format for {a['agent_id']}'s {a['action']}")
    case["from_rejection"] = origin
    out_dir = out_dir or CASES_DIR / agent / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{case['name']}.yaml"
    header = (f"# A candidate {agent} eval case from rejected approval {approval_id}"
              " (dip evals from-rejection).\n# Every cell is scrubbed to a synthetic value."
              " Check the gold and `TODO` fields, record it\n# live once, then move it into"
              " the agent's cases file with its recording.\n")  # fmt: skip
    text = yaml.dump({"cases": [case]}, Dumper=_Literal, sort_keys=False, width=100,
                     allow_unicode=True)  # fmt: skip
    path.write_text(header + text, encoding="utf-8", newline=NEWLINE)
    return path


def _triage_case(conn, store, contracts, a, p, seed) -> dict[str, Any]:
    feed, _, reason_code = str(p.get("group_id") or "").partition("|")
    c = _synthetic(feed, contracts)
    with conn.cursor() as cur:
        cur.execute("select distinct file_sha256 from ops.quarantine where quarantine_id = any(%s)",
                    ([int(i) for i in p.get("quarantine_ids") or []],))  # fmt: skip
        shas = [r[0] for r in cur.fetchall()]
    conn.commit()
    files = _raw_files(conn, store, feed, shas)
    if not files:
        raise FeedbackError(f"approval {a['approval_id']}: its quarantined files are not in raw/")
    delimiter = c.spec.get("format", {}).get("delimiter", ",")
    others = [x for x in ("request_resend", "discard", "replay", "propose_contract_change")
              if x != a["action"]]  # fmt: skip
    cause = p.get("root_cause") or "unknown"
    gold = {"root_cause": "TODO" if a["reason"] == "wrong_root_cause" else cause,
            "acceptable": others if a["reason"] in ("wrong_action", "unsafe") else [a["action"]],
            "preferred": "TODO"}  # fmt: skip
    code = reason_code.split(":")[0].lower().replace(".", "_")
    return {
        "name": f"rejected_{a['approval_id']}_{code}",
        "feed": feed,
        "files": {
            name: scrub_csv(_csv_text(c, data), c, seed + i, delimiter)
            for i, (name, data) in enumerate(files)
        },  # fmt: skip
        "gold": {reason_code: gold},
    }


def _author_case(conn, store, contracts, a, p, seed) -> dict[str, Any]:
    feed = p.get("feed") or ""
    if feed.partition(".")[0] not in SYNTHETIC_PACKS:
        raise FeedbackError(f"{feed}: not from a synthetic pack; a case is made only from"
                            " synthetic inputs")  # fmt: skip
    mode = p.get("mode") or "onboard"
    with conn.cursor() as cur:
        cur.execute("select file_sha256 from ops.intake_job where job_id = %s",
                    (p.get("intake_job_id"),))  # fmt: skip
        row = cur.fetchone()
        sha = row[0] if row else None
        cur.execute("select raw_key, name from ops.sample_file where feed = %s"
                    " and file_sha256 = %s", (feed, sha))  # fmt: skip
        sample = cur.fetchone()
    conn.commit()
    if sample is not None:
        name, data = sample[1], store.get(BUCKET_RAW, sample[0])
    else:
        files = _raw_files(conn, store, feed, [sha] if sha else [])
        if not files:
            raise FeedbackError(f"approval {a['approval_id']}: its sample is not in raw/")
        name, data = files[0]
    c = contracts.get(feed)
    if c is not None:
        _synthetic(feed, contracts)
    if not name.lower().endswith(".csv"):
        raise FeedbackError(f"{name}: only CSV samples are scrubbed cell by cell")
    text = scrub_csv(data.decode("utf-8", "replace"), c, seed)
    return {
        "name": f"rejected_{a['approval_id']}_{feed.replace('.', '_')}",
        "mode": mode,
        "feed": feed,
        "file": {"name": name, "text": text},
        "expect": {
            "propose": a["reason"] not in ("unsafe",),
            "checks": [],
            "TODO": (
                "say what the contract must (or must not) have: the rejected one is in"
                " from_rejection"
            ),
        },  # fmt: skip
    }
