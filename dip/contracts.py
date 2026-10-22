"""Load data contracts (YAML) and validate them against the contract JSON Schema."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from fnmatch import fnmatch
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
# In the images the package is installed, so they set DIP_CONTRACT_DIRS. The default is
# every domain pack's contracts (the MIMIC pack is local-only and loaded explicitly).
PACKS = ("healthcare", "finance")
DEFAULT_CONTRACT_DIRS = tuple(
    Path(p)
    for p in os.environ.get(
        "DIP_CONTRACT_DIRS",
        os.pathsep.join(str(REPO_ROOT / "packs" / pack / "contracts") for pack in PACKS),
    ).split(os.pathsep)
)


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Contract:
    feed: str
    version: int
    spec: dict[str, Any]
    path: Path

    @property
    def format_type(self) -> str:
        return self.spec["format"]["type"]

    @property
    def max_bad_row_ratio(self) -> float:
        return float(self.spec.get("max_bad_row_ratio", 0.0))

    @property
    def rejects_file_on_row_violation(self) -> bool:
        return self.spec["on_row_violation"] == "reject_file"

    def matches_filename(self, filename: str) -> bool:
        return fnmatch(filename, self.spec["source"]["filename_pattern"])

    def filename_date(self, filename: str, received: date) -> tuple[date | None, str | None]:
        """The business date in a file's name, or the problem with it.

        Returns (None, None) when the contract declares no `source.filename_date` or the
        name carries no date (the file then takes the date it was received). A name whose
        date is not a real date, is after the received date, or is too far before it,
        returns the problem, and the intake quarantines the file (FILE.NAME_DATE).
        """
        rule = self.spec["source"].get("filename_date")
        if not rule:
            return None, None
        m = re.search(rule["regex"], filename)
        if not m:
            return None, None
        text = m.group(1)
        try:
            day = datetime.strptime(text, rule["format"]).date()
        except ValueError:
            day = None
        # strptime accepts short fields ("2026102" as 2026-10-2): the text must round-trip.
        if day is None or day.strftime(rule["format"]) != text:
            return None, f"{text!r} in {filename!r} is not a real {rule['format']} date"
        after = rule.get("max_days_after_received", 0)
        before = rule.get("max_days_before_received", 7)
        if (day - received).days > after:
            return None, f"{day} is after the received date {received} (max {after} days)"
        if (received - day).days > before:
            return None, (
                f"{day} is {(received - day).days} days before the received date"
                f" {received} (max {before})"
            )
        return day, None


def dag_id_for(feed: str) -> str:
    """The intake DAG of a file feed: healthcare.eligibility -> intake__healthcare__eligibility."""
    return "intake__" + feed.replace(".", "__")


@cache
def _schema() -> dict[str, Any]:
    return json.loads((Path(__file__).parent / "contract.schema.json").read_text("utf-8"))


def load_contract(path: str | Path) -> Contract:
    path = Path(path)
    spec = yaml.safe_load(path.read_text("utf-8"))
    try:
        jsonschema.validate(spec, _schema())
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise ContractError(f"{path.name}: {where}: {exc.message}") from exc
    if rule := spec["source"].get("filename_date"):
        try:
            groups = re.compile(rule["regex"]).groups
        except re.error as exc:
            raise ContractError(f"{path.name}: source/filename_date/regex: {exc}") from exc
        if groups != 1:
            raise ContractError(
                f"{path.name}: source/filename_date/regex needs exactly one group, has {groups}"
            )
    if spec["format"]["type"] in ("csv", "excel"):
        from dip.csvfeed import check_contract

        if problems := check_contract(spec):
            raise ContractError(f"{path.name}: " + "; ".join(problems))
    return Contract(feed=spec["feed"], version=spec["version"], spec=spec, path=path)


def load_contracts(dirs: tuple[Path, ...] = DEFAULT_CONTRACT_DIRS) -> dict[str, Contract]:
    contracts: dict[str, Contract] = {}
    for d in dirs:
        for path in sorted(Path(d).glob("*.yaml")):
            c = load_contract(path)
            if c.feed in contracts:
                raise ContractError(
                    f"duplicate feed {c.feed} in {path} and {contracts[c.feed].path}"
                )
            contracts[c.feed] = c
    return contracts
