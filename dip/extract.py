"""Versioned research extracts of the shareable OMOP tier (R5).

`build` writes one CSV per OMOP table, a Frictionless Data Package descriptor
(`datapackage.json`: column definitions, license, provenance, checksums) and a
`manifest.sha256`. Everything is deterministic: rows are ordered by primary key, values
are formatted by Postgres as text, and the package holds no timestamps, so building
twice from the same raw inputs, code and key gives byte-identical files.

A published version is immutable: building it again either reproduces it exactly (and
says so) or fails, and the caller has to choose a new version.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dip import vocab
from dip.contracts import REPO_ROOT

SCHEMA = "omop_shareable"
# OMOP table -> primary key (row order in the CSV).
TABLES = {
    "person": "person_id",
    "observation_period": "observation_period_id",
    "visit_occurrence": "visit_occurrence_id",
    "condition_occurrence": "condition_occurrence_id",
    "drug_exposure": "drug_exposure_id",
    "measurement": "measurement_id",
    "observation": "observation_id",
}
PG_TO_FRICTIONLESS = {
    "bigint": "integer",
    "integer": "integer",
    "numeric": "number",
    "date": "date",
    "timestamp without time zone": "datetime",
    "character varying": "string",
    "text": "string",
}
MANIFEST = "manifest.sha256"
DESCRIPTOR = "datapackage.json"


class ExtractExistsError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildResult:
    path: Path
    reproduced_existing: bool
    manifest_sha256: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _query(conn: Any, sql: str, params: tuple = ()) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def _columns(conn: Any, table: str) -> list[tuple[str, str]]:
    return [
        (name, dtype)
        for name, dtype in _query(
            conn,
            "select column_name, data_type from information_schema.columns"
            " where table_schema = %s and table_name = %s order by ordinal_position",
            (SCHEMA, table),
        )
    ]


def _table_csv(conn: Any, table: str, pk: str, columns: list[tuple[str, str]]) -> bytes:
    select = ", ".join(f"{name}::text" for name, _ in columns)
    rows = _query(conn, f"select {select} from {SCHEMA}.{table} order by {pk}")
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([name for name, _ in columns])
    writer.writerows(["" if v is None else v for v in row] for row in rows)
    return buf.getvalue().encode("utf-8")


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _provenance(conn: Any, git_commit: str | None) -> dict[str, Any]:
    files = _query(
        conn,
        "select file_sha256, contract_version from ops.intake_file"
        " where status <> 'quarantined' order by file_sha256",
    )
    inputs_digest = _sha256("\n".join(sha for sha, _ in files).encode("utf-8"))
    (fingerprint,) = _query(
        conn, "select fingerprint from ops_secret.deid_key where key_id = 'default'"
    )[0]
    return {
        "source": "Synthea synthetic patients (FHIR R4), via the dip intake flow",
        "input_files": len(files),
        "inputs_sha256": inputs_digest,
        "contract_versions": sorted({v for _, v in files}),
        "code_git_commit": git_commit or _git_commit(),
        "vocabulary": {
            "name": "OHDSI Eunomia Synthea27Nj vocabulary shard",
            "commit": vocab.EUNOMIA_COMMIT,
            "sha256": vocab.EUNOMIA_SHA256,
        },
        "deid_key_fingerprint": fingerprint,
        "cdm_version": "5.4",
    }


def build_files(
    conn: Any, version: str, license_id: str, git_commit: str | None = None
) -> dict[str, bytes]:
    """All files of the package, in memory, keyed by name."""
    files: dict[str, bytes] = {}
    resources = []
    for table, pk in TABLES.items():
        columns = _columns(conn, table)
        if not columns:
            raise RuntimeError(f"{SCHEMA}.{table} does not exist; run `dbt build` first")
        data = _table_csv(conn, table, pk, columns)
        path = f"data/{table}.csv"
        files[path] = data
        resources.append(
            {
                "name": table,
                "path": path,
                "format": "csv",
                "mediatype": "text/csv",
                "encoding": "utf-8",
                "bytes": len(data),
                "hash": f"sha256:{_sha256(data)}",
                "schema": {
                    "fields": [
                        {"name": n, "type": PG_TO_FRICTIONLESS.get(t, "string")} for n, t in columns
                    ],
                    "primaryKey": [pk],
                },
            }
        )
    descriptor = {
        "profile": "data-package",
        "name": "dip-omop-shareable",
        "title": "De-identified OMOP CDM 5.4 research extract (synthetic)",
        "description": (
            "Synthetic patients generated by Synthea, ingested as FHIR, modeled as OMOP CDM "
            "5.4 and de-identified (keyed pseudonyms, per-person date shifts, year of birth "
            "only, ages 90+ top-coded). Contains no real patient data."
        ),
        "version": version,
        "licenses": [{"name": license_id}],
        "keywords": ["OMOP", "CDM", "FHIR", "Synthea", "synthetic", "research"],
        "provenance": _provenance(conn, git_commit),
        "resources": resources,
    }
    files[DESCRIPTOR] = (json.dumps(descriptor, indent=2, sort_keys=True) + "\n").encode("utf-8")
    files[MANIFEST] = "".join(f"{_sha256(files[name])}  {name}\n" for name in sorted(files)).encode(
        "utf-8"
    )
    return files


def build(
    conn: Any,
    out_root: Path,
    version: str,
    license_id: str = "CC0-1.0",
    git_commit: str | None = None,
) -> BuildResult:
    files = build_files(conn, version, license_id, git_commit)
    target = out_root / version
    manifest_sha = _sha256(files[MANIFEST])
    if target.exists():
        existing = target / MANIFEST
        if existing.is_file() and existing.read_bytes() == files[MANIFEST]:
            return BuildResult(target, True, manifest_sha)
        raise ExtractExistsError(
            f"extract {version} already exists at {target} with different content;"
            " published versions are immutable, so choose a new version"
        )
    tmp = out_root / f".{version}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    for name, data in files.items():
        p = tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    tmp.rename(target)
    return BuildResult(target, False, manifest_sha)


def diff(a: Path, b: Path) -> list[str]:
    """Names of files that differ between two extract directories (empty: identical)."""

    def listing(root: Path) -> dict[str, str]:
        return {
            p.relative_to(root).as_posix(): _sha256(p.read_bytes())
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    la, lb = listing(a), listing(b)
    return sorted(name for name in la.keys() | lb.keys() if la.get(name) != lb.get(name))
