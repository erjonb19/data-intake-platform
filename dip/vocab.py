"""OMOP vocabulary, loaded into `vocab` from one of two sources.

- The public OHDSI Eunomia shard (default, and what CI uses): about 2,300 concepts and their
  "Maps to" relationships from the Eunomia `Synthea27Nj` CDM 5.4 test dataset, Apache-2.0,
  built from Synthea codes. Codes it does not cover map to concept_id 0.
- A full OHDSI Athena download (`load_athena`), read from local folders. Athena files are
  licensed to the person who downloaded them and are never committed or redistributed;
  extracts carry concept ids only, never the vocabulary itself.

Either way `vocab.load_info` records what was loaded, for extract provenance.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import urllib.request
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

EUNOMIA_COMMIT = "ae167760e01dbfa1d5e3481c1f7f2aa054aff931"
EUNOMIA_URL = (
    "https://raw.githubusercontent.com/OHDSI/EunomiaDatasets/"
    f"{EUNOMIA_COMMIT}/datasets/Synthea27Nj/Synthea27Nj_5.4.zip"
)
EUNOMIA_SHA256 = "cdd3971be325e17b0e872bf4560711ab4c346917ecb079e2faf32badb55cb693"
CACHE_DIR = Path(os.environ.get("DIP_CACHE_DIR", "data/cache"))
EUNOMIA_PROVENANCE = {
    "name": "OHDSI Eunomia Synthea27Nj vocabulary shard",
    "commit": EUNOMIA_COMMIT,
    "sha256": EUNOMIA_SHA256,
}

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
DROP TABLE IF EXISTS vocab.load_info;
-- json, not jsonb: keeps key order, so extract descriptors stay byte-stable.
CREATE TABLE vocab.load_info (provenance json NOT NULL);
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
# OMOP SOURCE_TO_CONCEPT_MAP: site-maintained maps for local codes (e.g. MIMIC lab items).
# Kept across vocabulary reloads; empty unless a map is loaded.
STCM_DDL = """
CREATE SCHEMA IF NOT EXISTS vocab;
CREATE TABLE IF NOT EXISTS vocab.source_to_concept_map (
    source_code varchar(50) NOT NULL, source_concept_id integer NOT NULL,
    source_vocabulary_id varchar(20) NOT NULL, source_code_description varchar(255),
    target_concept_id integer NOT NULL, target_vocabulary_id varchar(20) NOT NULL,
    valid_start_date date NOT NULL, valid_end_date date NOT NULL, invalid_reason varchar(1)
);
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
        result = cur.fetchone()[0]
        if not result:
            cur.execute("select not exists (select 1 from vocab.concept)")
            result = cur.fetchone()[0]
    conn.commit()  # end the read transaction so it holds no locks while dbt runs
    return result


def _record(cur: Any, provenance: dict[str, Any]) -> None:
    cur.execute("insert into vocab.load_info values (%s)", (json.dumps(provenance),))


def provenance(conn: Any) -> dict[str, Any]:
    """What `vocab` holds. Warehouses loaded before load_info existed hold Eunomia."""
    with conn.cursor() as cur:
        cur.execute("select to_regclass('vocab.load_info') is not null")
        row = None
        if cur.fetchone()[0]:
            cur.execute("select provenance::text from vocab.load_info")
            row = cur.fetchone()
    conn.commit()
    return json.loads(row[0]) if row else dict(EUNOMIA_PROVENANCE)


def load(conn: Any, zip_bytes: bytes) -> tuple[int, int]:
    concepts, maps_to = read_tables(zip_bytes)
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(STCM_DDL)
            _insert(cur, "vocab.concept", CONCEPT_COLUMNS, concepts)
            _insert(cur, "vocab.concept_relationship", RELATIONSHIP_COLUMNS, maps_to)
            _record(cur, EUNOMIA_PROVENANCE)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(concepts), len(maps_to)


# --- Full Athena download -------------------------------------------------------------

# Athena files are tab-separated with no quoting; any quote character is literal. COPY's
# csv format with an impossible quote character reads them as-is (text format would
# treat backslashes as escapes).
_COPY_OPTS = "(format csv, delimiter E'\\t', quote E'\\x01', header false)"
_MAPS_TO = b"\tMaps to\t"


def athena_dirs(value: str | None = None) -> list[Path]:
    """Folders from DIP_ATHENA_DIRS (os.pathsep-separated), in load order."""
    value = os.environ.get("DIP_ATHENA_DIRS", "") if value is None else value
    return [Path(p) for p in value.split(os.pathsep) if p.strip()]


def _lines(path: Path, columns: tuple[str, ...], must_contain: bytes | None) -> Iterator[bytes]:
    """Data lines of one Athena file, in ~1 MiB chunks, after checking its header."""
    with path.open("rb") as f:
        header = tuple(f.readline().decode("utf-8-sig").rstrip("\r\n").split("\t"))
        if header[: len(columns)] != columns:
            raise ValueError(f"{path}: unexpected header {header}")
        buf: list[bytes] = []
        size = 0
        for line in f:
            if must_contain is None or must_contain in line:
                buf.append(line)
                size += len(line)
                if size >= 1 << 20:
                    yield b"".join(buf)
                    buf, size = [], 0
        if buf:
            yield b"".join(buf)


def _copy(cur: Any, table: str, columns: tuple[str, ...], chunks: Iterator[bytes]) -> None:
    with cur.copy(f"copy {table} ({','.join(columns)}) from stdin {_COPY_OPTS}") as cp:
        for chunk in chunks:
            cp.write(chunk)


def _vocabularies(dirs: Sequence[Path]) -> tuple[str | None, list[str]]:
    """(Athena release, vocabulary ids) from the VOCABULARY.csv files."""
    release, ids = None, set()
    for d in dirs:
        with (d / "VOCABULARY.csv").open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                ids.add(row["vocabulary_id"])
                if row["vocabulary_id"] == "None":
                    release = row["vocabulary_version"]
    return release, sorted(ids)


def load_athena(conn: Any, dirs: Sequence[Path]) -> tuple[int, int]:
    """Load CONCEPT and the "Maps to" rows of CONCEPT_RELATIONSHIP from one or more
    Athena download folders (psycopg 3). Folders may overlap (every download repeats the
    base vocabularies): rows are de-duplicated, and the first folder wins a conflict."""
    if not dirs:
        raise ValueError("no Athena folders given (DIP_ATHENA_DIRS)")
    for d in dirs:
        for name in ("CONCEPT.csv", "CONCEPT_RELATIONSHIP.csv", "VOCABULARY.csv"):
            if not (d / name).is_file():
                raise FileNotFoundError(d / name)
    release, vocabularies = _vocabularies(dirs)
    cols, rcols = ",".join(CONCEPT_COLUMNS), ",".join(RELATIONSHIP_COLUMNS)
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(STCM_DDL)
            # Staging without constraints; `seq` keeps the folder order for de-duplication.
            cur.execute(
                "create temp table athena_concept (like vocab.concept) on commit drop;"
                "alter table athena_concept add column seq bigserial;"
                "create temp table athena_rel (like vocab.concept_relationship) on commit drop"
            )
            for d in dirs:
                _copy(
                    cur,
                    "athena_concept",
                    CONCEPT_COLUMNS,
                    _lines(d / "CONCEPT.csv", CONCEPT_COLUMNS, None),
                )
                _copy(
                    cur,
                    "athena_rel",
                    RELATIONSHIP_COLUMNS,
                    _lines(d / "CONCEPT_RELATIONSHIP.csv", RELATIONSHIP_COLUMNS, _MAPS_TO),
                )
            cur.execute(
                f"insert into vocab.concept ({cols}) select distinct on (concept_id) {cols}"
                " from athena_concept order by concept_id, seq"
            )
            concepts = cur.rowcount
            cur.execute(
                f"insert into vocab.concept_relationship ({rcols})"
                f" select distinct on (concept_id_1, concept_id_2) {rcols}"
                " from athena_rel where relationship_id = 'Maps to'"
                " order by concept_id_1, concept_id_2"
            )
            maps_to = cur.rowcount
            _record(cur, {"name": "OHDSI Athena", "release": release, "vocabularies": vocabularies})
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("analyze vocab.concept")
            cur.execute("analyze vocab.concept_relationship")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return concepts, maps_to


def load_configured(conn: Any, cache_dir: Path = CACHE_DIR) -> dict[str, Any]:
    """Athena when DIP_ATHENA_DIRS is set, else the Eunomia shard. An Athena load is
    skipped when the same release and vocabularies are already loaded."""
    dirs = athena_dirs()
    if not dirs:
        load(conn, fetch(cache_dir))
    else:
        release, vocabularies = _vocabularies(dirs)
        wanted = {"name": "OHDSI Athena", "release": release, "vocabularies": vocabularies}
        if needs_load(conn) or provenance(conn) != wanted:
            load_athena(conn, dirs)
    ensure_source_to_concept_map(conn)
    return provenance(conn)


# --- Source-to-concept maps -----------------------------------------------------------


def ensure_source_to_concept_map(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(STCM_DDL)
    conn.commit()


# MIT-LCP mimic-code (MIT license): MIMIC-IV lab itemid -> LOINC, pinned by tag commit.
MIMIC_CODE_COMMIT = "570ef01990566b6717e9f460f2bab92579776b1d"  # tag v2.4.0
MIMIC_LAB_MAP_URL = (
    f"https://raw.githubusercontent.com/MIT-LCP/mimic-code/{MIMIC_CODE_COMMIT}"
    "/mimic-iv/concepts/concept_map/d_labitems_to_loinc.csv"
)
MIMIC_LAB_MAP_SHA256 = "f7ef583bb2669cbf8f450e1b629dd83161c1d3f2f3ebe6abd2ff8111570e86b8"
MIMIC_LAB_VOCABULARY = "MIMIC_LAB"


def fetch_mimic_lab_map(cache_dir: Path = CACHE_DIR) -> bytes:
    path = cache_dir / f"mimic-code-{MIMIC_CODE_COMMIT[:12]}-d_labitems_to_loinc.csv"
    if path.is_file():
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(MIMIC_LAB_MAP_URL, timeout=120) as resp:
            data = resp.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != MIMIC_LAB_MAP_SHA256:
        raise ValueError(f"mimic-code lab map checksum mismatch: got {actual}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def read_mimic_lab_map(data: bytes) -> list[tuple]:
    """source_to_concept_map rows for the items mimic-code maps to a concept. A row whose
    concept id is not an integer is skipped, not guessed (v2.4.0 has one: itemid 52434
    carries its LOINC code in the concept id column)."""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    rows = []
    for r in reader:
        target = (r.get("omop_concept_id") or "").strip()
        if not target.isdigit() or target == "0":
            continue
        rows.append(
            (
                r["itemid (omop_source_code)"].strip(),
                0,
                MIMIC_LAB_VOCABULARY,
                (r.get("label") or "")[:255] or None,
                int(target),
                r.get("omop_vocabulary_id") or "LOINC",
                "1970-01-01",
                "2099-12-31",
                None,
            )
        )
    return rows


STCM_COLUMNS = (
    "source_code",
    "source_concept_id",
    "source_vocabulary_id",
    "source_code_description",
    "target_concept_id",
    "target_vocabulary_id",
    "valid_start_date",
    "valid_end_date",
    "invalid_reason",
)


def load_source_to_concept_map(conn: Any, vocabulary_id: str, rows: list[tuple]) -> int:
    """Replace one source vocabulary's rows in vocab.source_to_concept_map."""
    try:
        with conn.cursor() as cur:
            cur.execute(STCM_DDL)
            cur.execute(
                "delete from vocab.source_to_concept_map where source_vocabulary_id = %s",
                (vocabulary_id,),
            )
            _insert(cur, "vocab.source_to_concept_map", STCM_COLUMNS, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


# Source vocabularies the pipeline maps codes from today (conditions: ICD9CM, ICD10CM,
# ICD10; drugs: NDC; procedures are SNOMED, which is standard). Many others
# (MeSH, CCAM, Mondo, ...) legitimately have no "Maps to" for most concepts.
MAPPED_SOURCE_VOCABULARIES = ("ICD9CM", "ICD10", "ICD10CM", "NDC")


def mapping_gaps(conn: Any) -> dict[str, dict[str, int]]:
    """The source vocabularies the pipeline maps from (MAPPED_SOURCE_VOCABULARIES) whose
    valid concepts mostly have no "Maps to".

    Athena exports a relationship only when both of its concepts are in the same download,
    so loading SNOMED and ICD10CM from separate downloads leaves ICD10CM without its
    mappings to SNOMED. This makes that visible right after a load."""
    with conn.cursor() as cur:
        cur.execute(
            "select c.vocabulary_id, count(*) filter (where c.standard_concept = 'S'),"
            " count(*) filter (where c.standard_concept is null),"
            " count(*) filter (where c.standard_concept is null and not exists ("
            "   select 1 from vocab.concept_relationship r"
            "   where r.concept_id_1 = c.concept_id and r.relationship_id = 'Maps to'))"
            " from vocab.concept c where c.invalid_reason is null"
            " and c.vocabulary_id = any(%s) group by 1 order by 1",
            (list(MAPPED_SOURCE_VOCABULARIES),),
        )
        out = {
            v: {"non_standard": n, "without_maps_to": gap}
            for v, std, n, gap in cur.fetchall()
            if n and std * 20 < n and gap * 2 > n
        }
    conn.commit()
    return out
