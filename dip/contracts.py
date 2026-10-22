"""Load data contracts (YAML) and validate them against the contract JSON Schema."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
# In the Airflow image the package is installed, so the image sets DIP_CONTRACT_DIRS.
DEFAULT_CONTRACT_DIRS = tuple(
    Path(p)
    for p in os.environ.get(
        "DIP_CONTRACT_DIRS", str(REPO_ROOT / "packs" / "healthcare" / "contracts")
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
    if spec["format"]["type"] == "csv":
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
