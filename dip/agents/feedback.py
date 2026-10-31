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
2026-10-31). The person's note is never copied (it may name a member), and file names are
rebuilt from the contract's `filename_pattern` keeping only a valid date. Each data cell
keeps its shape (letters, digits, punctuation) with new characters,
and stays on the same side of every contract rule its column has, so the case quarantines
for the same reasons as the original: a value matching the column's `pattern` still
matches it (one that broke it still breaks it), a date within `min`..`max` stays within
(one outside stays outside, on the same side), and a value from the `allowed` list stays
(it is the contract's, not the vendor's; one not allowed is replaced by another not
allowed); a number keeps its side of the column's min and max, and two different values of
a unique column never become one. Column names are kept when the contract says the file
has a header row: they are the schema the case tests. The command refuses a proposal whose
contract is not in a synthetic pack's `contracts/` (a MIMIC-backed run, a contract from
anywhere else, a new vendor with no contract yet), a format it cannot scrub cell by cell
(only CSV), a fault in the file's bytes or framing (encoding, quoting), and a value it
cannot replace while keeping its rules.
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
    same side of each rule: its allowed list, pattern, unique flag, and its min and max
    (dates for a date column, numbers for an integer or decimal one)."""

    column: str = ""
    allowed: frozenset[str] = frozenset()
    pattern: re.Pattern | None = None
    min: Any = None
    max: Any = None
    numeric: bool = False
    unique: bool = False

    @classmethod
    def of(cls, col: dict[str, Any]) -> Rule:
        numeric = col.get("type") in ("integer", "decimal")

        def bound(v: Any) -> Any:
            if v is None or v == "":
                return None
            if numeric:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            if v == "today":
                return date.today()
            try:
                return date.fromisoformat(str(v))
            except ValueError:
                return None

        p = col.get("pattern")
        return cls(col.get("name", ""), frozenset(str(v) for v in col.get("allowed") or []),
                   re.compile(p) if p else None, bound(col.get("min")), bound(col.get("max")),
                   numeric, bool(col.get("unique")))  # fmt: skip

    def side(self, value: str) -> tuple:
        """Which side of each rule the value is on; a scrubbed value must be on the same."""
        pattern = self.pattern.fullmatch(value) is not None if self.pattern else None
        rank = None
        try:
            if self.numeric and (self.min is not None or self.max is not None):
                x = float(value)
                rank = (
                    self.min is not None and x < self.min,
                    self.max is not None and x > self.max,
                )
            elif not self.numeric and DATE.match(value) and (self.min or self.max):
                d = date.fromisoformat(value)
                rank = (
                    self.min is not None and d < self.min,
                    self.max is not None and d > self.max,
                )
        except ValueError:
            rank = "unreadable"
        return (value in self.allowed, pattern, rank)


class Scrubber:
    """Shape-keeping replacements, the same for the same value in the same column within
    one case (so a key that repeats still repeats), never the same for two different values
    of a unique column, seeded so a case is reproducible."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.seen: dict[tuple[str, str], str] = {}
        self.taken: dict[str, set[str]] = {}

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

    def _digits(self, value: str) -> str:
        return "".join(self.rng.choice("0123456789") if c.isdigit() else c for c in value)

    def _bounded_date(self, value: str, rule: Rule) -> str:
        """A random date on the same side of the column's min and max as the original."""
        d = date.fromisoformat(value)
        lo = rule.min if isinstance(rule.min, date) else date(1950, 1, 1)
        hi = rule.max if isinstance(rule.max, date) else date.today()
        if d < lo:
            lo, hi = lo - timedelta(days=3650), lo - timedelta(days=1)
        elif d > hi:
            lo, hi = hi + timedelta(days=1), hi + timedelta(days=3650)
        return (lo + timedelta(days=self.rng.randrange(max((hi - lo).days, 0) + 1))).isoformat()

    def _candidates(self, value: str, rule: Rule):
        """Whole-value replacements first; a pattern with fixed letters (an id like
        `M000000011`) may need its letters kept, so digits-only replacements come last."""
        for i in range(400):
            if DATE.match(value) and not rule.numeric and i < 200:
                try:
                    yield self._bounded_date(value, rule)
                    continue
                except ValueError:
                    pass
            yield self._chars(value) if i < 300 else self._digits(value)

    def value(self, value: str, rule: Rule | None = None) -> str:
        rule = rule or Rule()
        if not value or value in rule.allowed or not any(ch.isalnum() for ch in value):
            return value
        key = (rule.column, value)
        if key in self.seen:
            return self.seen[key]
        want = rule.side(value)
        taken = self.taken.setdefault(rule.column, set())
        for new in self._candidates(value, rule):
            if (new != value and rule.side(new) == want
                    and not (rule.unique and new in taken)):  # fmt: skip
                break
        else:
            if rule.numeric:  # a bare number (0 under a minimum of 1) identifies no one
                new = value
                self.seen[key] = new
                return new
            col = rule.column or "cell"
            raise FeedbackError(f"cannot scrub a {col} value and keep its contract rules as"
                                " they were: write this case by hand")  # fmt: skip
        self.seen[key] = new
        taken.add(new)
        return new


def scrub_csv(text: str, contract: Contract | None, seed: int, delimiter: str = ",") -> str:
    """Every data cell scrubbed. The header row (the schema) is kept only when the
    contract says the file has one; without a contract, or with `header: false`, the first
    row is data and is scrubbed too."""
    columns = contract.spec.get("columns", []) if contract else []
    rules = [Rule.of(c) for c in columns]
    by_name = {r.column: r for r in rules}
    has_header = bool(contract) and contract.spec.get("format", {}).get("header", True)
    s = Scrubber(seed)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        return text
    if has_header:
        header, out, body = rows[0], [rows[0]], rows[1:]
    else:
        header, out, body = [r.column for r in rules], [], rows
    for row in body:
        out.append([s.value(v, by_name.get(header[i]) if i < len(header) else None)
                    for i, v in enumerate(row)])  # fmt: skip
    buf = io.StringIO()
    csv.writer(buf, delimiter=delimiter, lineterminator="\n").writerows(out)
    return buf.getvalue()


def scrub_name(name: str, contract: Contract | None, seed: int) -> str:
    """A file name the contract accepts, with nothing of the vendor's but a valid date: the
    contract's `filename_pattern` with its `*` replaced by the original's date (YYYYMMDD,
    if the name has a valid one) and scrubbed letters and digits around it."""
    pattern = ((contract.spec.get("source") or {}).get("filename_pattern")
               if contract else None)  # fmt: skip
    rng = random.Random(seed)
    dates = [m for m in re.findall(r"(?<!\d)(\d{8})(?!\d)", name) if _yyyymmdd(m)]
    middle = dates[0] if dates else "".join(rng.choice("0123456789") for _ in range(6))
    if not pattern or pattern.count("*") != 1:
        return f"sample_{middle}{Path(name).suffix.lower()}"
    return pattern.replace("*", middle)


def _yyyymmdd(s: str) -> bool:
    try:
        date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        return False
    return True


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
        raise FeedbackError(f"{feed}: no contract, so nothing shows its inputs are synthetic")
    # Where the contract lives, not only its feed's name: a contract loaded from anywhere
    # else (DIP_CONTRACT_DIRS) is not one of the synthetic packs'.
    homes = [(REPO_ROOT / "packs" / pack / "contracts").resolve() for pack in SYNTHETIC_PACKS]
    if not any(Path(c.path).resolve().is_relative_to(h) for h in homes):
        raise FeedbackError(f"{feed}: its contract is not in a synthetic pack's contracts/")
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
    try:
        return data.decode(fmt.get("encoding", "utf-8"))
    except UnicodeDecodeError as exc:  # its fault is in its bytes: scrubbing would lose it
        raise FeedbackError(f"{c.feed}: the file is not {fmt.get('encoding', 'utf-8')}; write"
                            " this case by hand") from exc  # fmt: skip


def from_rejection(conn: Any, store: Any, contracts: dict[str, Contract], approval_id: int,
                   out_dir: Path | None = None) -> Path:  # fmt: skip
    """Write the candidate case and return its path."""
    a = _approval(conn, approval_id)
    p = a["payload"] or {}
    seed = approval_id
    origin = {"approval_id": approval_id, "rejected_action": a["action"],
              "reason": a["reason"],
              # The person's note is free text that may name a member: never copied.
              "note": "TODO: restate the note without any value from the data"
                      if a["note"] else None,
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
    if any(w in reason_code for w in ("ENCODING", "PARSE", "DELIMITER", "QUOTE")):
        # the fault is in the file's bytes or framing, which a cell-by-cell rewrite loses
        raise FeedbackError(f"{reason_code}: a framing fault; write this case by hand")
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
            scrub_name(name, c, seed + i): scrub_csv(_csv_text(c, data), c, seed + i, delimiter)
            for i, (name, data) in enumerate(files)
        },  # fmt: skip
        "gold": {reason_code: gold},
    }


def _author_case(conn, store, contracts, a, p, seed) -> dict[str, Any]:
    feed = p.get("feed") or ""
    mode = p.get("mode") or "onboard"
    # A drift job's feed has a contract that shows where its files come from; a new
    # vendor's sample has none, so nothing shows it is synthetic: write that case by hand.
    c = _synthetic(feed, contracts)
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
    if not name.lower().endswith(".csv"):
        raise FeedbackError("only CSV samples are scrubbed cell by cell")
    text = scrub_csv(_csv_text(c, data), c, seed, c.spec.get("format", {}).get("delimiter", ","))
    return {
        "name": f"rejected_{a['approval_id']}_{feed.replace('.', '_')}",
        "mode": mode,
        "feed": feed,
        "file": {"name": scrub_name(name, c, seed), "text": text},
        "expect": {
            "propose": a["reason"] not in ("unsafe",),
            "checks": [],
            "TODO": (
                "say what the contract must (or must not) have: the rejected one is in"
                " from_rejection"
            ),
        },  # fmt: skip
    }
