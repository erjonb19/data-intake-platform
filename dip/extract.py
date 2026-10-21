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


class LicenseError(ValueError):
    pass


SYNTHETIC_LICENSE = "CC0-1.0"
MIMIC_LICENSE = "ODbL-1.0"
ODBL = {
    "name": MIMIC_LICENSE,
    "path": "https://opendatacommons.org/licenses/odbl/1-0/",
    "title": "Open Data Commons Open Database License v1.0",
}
# Attribution PhysioNet requires for MIMIC-IV demo derivatives (see docs/research/mimic.md).
MIMIC_SOURCES = [
    {
        "title": "Bennett A, Ulrich H, Wiedekopf J, Szul P, Grimes J, Johnson A (2025). "
        "MIMIC-IV Clinical Database Demo on FHIR (version 2.1.0). PhysioNet.",
        "path": "https://doi.org/10.13026/vphg-y548",
    },
    {
        "title": "Johnson A, Bulgarelli L, Pollard T, Horng S, Celi LA, Mark R (2023). "
        "MIMIC-IV Clinical Database Demo (version 2.2). PhysioNet.",
        "path": "https://doi.org/10.13026/dp1f-ex47",
    },
    {
        "title": "Johnson A, Bulgarelli L, Pollard T, Celi LA, Horng S, Mark R (2023). "
        "MIMIC-IV-ED Demo (version 2.2). PhysioNet.",
        "path": "https://doi.org/10.13026/jzz5-vs76",
    },
    {
        "title": "Bennett AM, Ulrich H, van Damme P, Wiedekopf J, Johnson AE. MIMIC-IV on FHIR. "
        "JAMIA 2023;30(4):718-25.",
        "path": "https://doi.org/10.1093/jamia/ocad002",
    },
    {
        "title": "Pollard T, Moody BE, Lehman L, et al. (2026). PhysioNet as a global platform "
        "for biomedical research. Nature Health.",
        "path": "https://doi.org/10.1038/s44360-026-00096-z",
    },
]


def _sources(conn: Any) -> list[tuple[str, str]]:
    """(source name, input sha256) for everything that fed this warehouse: intake feeds
    and relational source tables."""
    rows = list(
        _query(conn, "select feed, file_sha256 from ops.intake_file where status <> 'quarantined'")
    )
    if _query(conn, "select to_regclass('ops.relational_load') is not null")[0][0]:
        rows += _query(
            conn,
            "select source || '.' || table_name, file_sha256 from ops.relational_load"
            " where status = 'loaded'",
        )
    return sorted(((n, sha) for n, sha in rows), key=lambda r: (r[1], r[0]))


def is_mimic(sources: list[tuple[str, str]]) -> bool:
    return any(name.startswith("mimic.") for name, _ in sources)


def resolve_license(requested: str | None, mimic: bool) -> str:
    """CC0 for synthetic data by default. MIMIC derivatives must be ODbL (share-alike,
    with attribution): CC0 or any other license is refused."""
    if not mimic:
        return requested or SYNTHETIC_LICENSE
    if requested not in (None, MIMIC_LICENSE):
        raise LicenseError(
            f"this extract is derived from MIMIC-IV (ODbL); it must be {MIMIC_LICENSE} with "
            f"attribution, not {requested!r}"
        )
    return MIMIC_LICENSE


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


def _provenance(conn: Any, git_commit: str | None, sources: list[tuple[str, str]]) -> dict:
    files = _query(
        conn,
        "select file_sha256, contract_version from ops.intake_file"
        " where status <> 'quarantined' order by file_sha256",
    )
    inputs = [sha for _, sha in sources]
    inputs_digest = _sha256("\n".join(inputs).encode("utf-8"))
    (fingerprint,) = _query(
        conn, "select fingerprint from ops_secret.deid_key where key_id = 'default'"
    )[0]
    return {
        "source": (
            "MIMIC-IV demo (real, de-identified; PhysioNet, ODbL)"
            if is_mimic(sources)
            else "Synthea synthetic patients (FHIR R4), via the dip intake flow"
        ),
        "source_feeds": sorted({name for name, _ in sources}),
        "input_files": len(inputs),
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
    conn: Any, version: str, license_id: str | None = None, git_commit: str | None = None
) -> dict[str, bytes]:
    """All files of the package, in memory, keyed by name."""
    sources = _sources(conn)
    mimic = is_mimic(sources)
    license_id = resolve_license(license_id, mimic)
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
    if mimic:
        descriptor = {
            "profile": "data-package",
            "name": "dip-omop-shareable-mimic-iv-demo",
            "title": "De-identified OMOP CDM 5.4 extract derived from the MIMIC-IV demo",
            "description": (
                "REAL, de-identified patient records from the MIMIC-IV demo (PhysioNet), "
                "modeled as OMOP CDM 5.4 and de-identified again (keyed pseudonyms, "
                "per-person date shifts, year of birth only, ages 90+ top-coded). A derivative "
                "of an ODbL database: share-alike and attribution apply. Kept local; not for "
                "publication without the data owner's decision."
            ),
            "version": version,
            "licenses": [ODBL],
            "sources": MIMIC_SOURCES,
            "keywords": ["OMOP", "CDM", "MIMIC-IV", "de-identified", "research"],
            "dip_distribution": "local-only",
        }
    else:
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
        }
    descriptor["provenance"] = _provenance(conn, git_commit, sources)
    descriptor["resources"] = resources
    files[DESCRIPTOR] = (json.dumps(descriptor, indent=2, sort_keys=True) + "\n").encode("utf-8")
    files[MANIFEST] = "".join(f"{_sha256(files[name])}  {name}\n" for name in sorted(files)).encode(
        "utf-8"
    )
    return files


def build(
    conn: Any,
    out_root: Path,
    version: str,
    license_id: str | None = None,
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
