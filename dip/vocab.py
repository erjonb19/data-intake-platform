"""OMOP vocabulary: download the public OHDSI Eunomia shard and load it into `vocab`.

The shard (about 2,300 concepts and their "Maps to" relationships) ships with the Eunomia
`Synthea27Nj` CDM 5.4 test dataset, Apache-2.0, built from Synthea codes. It is not a full
Athena vocabulary: codes it does not cover map to concept_id 0, and the DQ checks report
that rate. A full Athena download (free account) can be loaded into the same tables.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

EUNOMIA_COMMIT = "ae167760e01dbfa1d5e3481c1f7f2aa054aff931"
EUNOMIA_URL = (
    "https://raw.githubusercontent.com/OHDSI/EunomiaDatasets/"
    f"{EUNOMIA_COMMIT}/datasets/Synthea27Nj/Synthea27Nj_5.4.zip"
)
EUNOMIA_SHA256 = "cdd3971be325e17b0e872bf4560711ab4c346917ecb079e2faf32badb55cb693"
CACHE_DIR = Path(os.environ.get("DIP_CACHE_DIR", "data/cache"))

CONCEPT_COLUMNS = (
    "concept_id",
    "concept_name",
    "domain_id",
    "vocabulary_id",
    "concept_class_id",
    "standard_concept",
    "concept_code",
    "valid_start_date",
    "valid_end_date",
    "invalid_reason",
)
RELATIONSHIP_COLUMNS = (
    "concept_id_1",
    "concept_id_2",
    "relationship_id",
    "valid_start_date",
    "valid_end_date",
    "invalid_reason",
)

DDL = """
CREATE SCHEMA IF NOT EXISTS vocab;
DROP TABLE IF EXISTS vocab.concept_relationship;
DROP TABLE IF EXISTS vocab.concept;
CREATE TABLE vocab.concept (
    concept_id integer PRIMARY KEY, concept_name varchar(255) NOT NULL,
    domain_id varchar(20) NOT NULL, vocabulary_id varchar(20) NOT NULL,
    concept_class_id varchar(20) NOT NULL, standard_concept varchar(1),
    concept_code varchar(50) NOT NULL, valid_start_date date NOT NULL,
    valid_end_date date NOT NULL, invalid_reason varchar(1)
);
CREATE INDEX concept_code_idx ON vocab.concept (vocabulary_id, concept_code);
CREATE TABLE vocab.concept_relationship (
    concept_id_1 integer NOT NULL, concept_id_2 integer NOT NULL,
    relationship_id varchar(20) NOT NULL, valid_start_date date NOT NULL,
    valid_end_date date NOT NULL, invalid_reason varchar(1)
);
CREATE INDEX concept_relationship_idx ON vocab.concept_relationship (concept_id_1, relationship_id);
"""


def fetch(cache_dir: Path) -> bytes:
    """The pinned Eunomia zip, from the cache or the network, checked by sha256."""
    path = cache_dir / f"eunomia-synthea27nj-{EUNOMIA_COMMIT[:12]}.zip"
    if path.is_file():
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(EUNOMIA_URL, timeout=120) as resp:
            data = resp.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != EUNOMIA_SHA256:
        raise ValueError(f"Eunomia checksum mismatch: got {actual}, expected {EUNOMIA_SHA256}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def read_tables(zip_bytes: bytes) -> tuple[list[tuple], list[tuple]]:
    """CONCEPT rows and the "Maps to" rows of CONCEPT_RELATIONSHIP (all we use)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:

        def rows(name: str, columns: tuple[str, ...]) -> list[tuple]:
            member = next(n for n in zf.namelist() if n.endswith(name) and "__MACOSX" not in n)
            with zf.open(member) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
                return [tuple(r[c] or None for c in columns) for r in reader]

        concepts = rows("CONCEPT.csv", CONCEPT_COLUMNS)
        maps_to = [
            r for r in rows("CONCEPT_RELATIONSHIP.csv", RELATIONSHIP_COLUMNS) if r[2] == "Maps to"
        ]
    return concepts, maps_to


def _insert(cur: Any, table: str, columns: tuple[str, ...], rows: list[tuple], batch=1000) -> None:
    # Multi-row VALUES batches: fast on both psycopg 3 and Airflow's psycopg2.
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        placeholders = ",".join(["(" + ",".join(["%s"] * len(columns)) + ")"] * len(chunk))
        cur.execute(
            f"insert into {table} ({','.join(columns)}) values {placeholders}",
            [v for row in chunk for v in row],
        )


def needs_load(conn: Any) -> bool:
    with conn.cursor() as cur:
        cur.execute("select to_regclass('vocab.concept') is null")
        if cur.fetchone()[0]:
            return True
        cur.execute("select not exists (select 1 from vocab.concept)")
        return cur.fetchone()[0]


def load(conn: Any, zip_bytes: bytes) -> tuple[int, int]:
    concepts, maps_to = read_tables(zip_bytes)
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            _insert(cur, "vocab.concept", CONCEPT_COLUMNS, concepts)
            _insert(cur, "vocab.concept_relationship", RELATIONSHIP_COLUMNS, maps_to)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(concepts), len(maps_to)
