"""M2: relational tables -> contract checks -> the same OMOP models (source_kind=relational).

Uses the synthetic MIMIC-shaped tables in tests/fixtures/mimic_shaped (no MIMIC data).
"""

from __future__ import annotations

import gzip
import os
from datetime import date
from pathlib import Path

import pytest

from dip import relational, vocab
from dip.dbt_runner import run_dbt
from dip.deid import install_key
from tests.fixtures.mimic_shaped import make_relational
from tests.integration.conftest import DSN, _fresh_warehouse, rows

CACHE = Path(os.environ.get("DIP_CACHE_DIR", "data/cache"))
VARS = ["--vars", "{source_kind: mimic_relational}"]


@pytest.fixture(scope="module")
def omop(tmp_path_factory):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    wh = _fresh_warehouse()
    with wh.conn.cursor() as cur:
        cur.execute("drop schema if exists mimic cascade")
    wh.conn.commit()
    root = make_relational.write(tmp_path_factory.mktemp("rel"))
    results = relational.load_all(wh.conn, make_relational.CONTRACT, root)
    assert {r.status for r in results} == {"loaded"}, results
    vocab.load(wh.conn, vocab.fetch(CACHE))
    install_key(wh.conn, "ci-test-key-0123456789abcdef0123456789abcdef")
    assert run_dbt(DSN, "seed", *VARS, check=False).returncode == 0
    assert run_dbt(DSN, "run", *VARS, check=False).returncode == 0
    yield wh
    wh.conn.close()
    # Leave the default (FHIR) models in place for the next module.
    wh2 = _fresh_warehouse()
    with wh2.conn.cursor() as cur:
        cur.execute("drop schema if exists mimic cascade")
    wh2.conn.commit()
    wh2.conn.close()


def test_omop_from_relational_tables(omop):
    assert rows(
        omop, "select year_of_birth, gender_concept_id, race_concept_id from omop.person"
    ) == [(2091, 8532, 8527)]
    assert rows(
        omop,
        "select visit_concept_id, visit_source_value, visit_start_datetime::text"
        " from omop.visit_occurrence",
    ) == [(9201, "EW EMER.", "2155-03-01 15:00:00")]  # Boston 10:00 EST is 15:00 UTC
    assert rows(
        omop,
        "select condition_source_value, condition_start_date from omop.condition_occurrence"
        " order by 1",
    ) == [("401.9", date(2155, 3, 1)), ("E11.9", date(2155, 3, 1))]
    assert rows(omop, "select count(*) from omop.measurement") == [(2,)]
    assert rows(omop, "select drug_source_value from omop.drug_exposure order by 1") == [
        ("00000000001",),
        ("Mix 1",),
    ]


def _with_header(root: Path, table: str, transform) -> None:
    spec = make_relational.CONTRACT["tables"][table]
    path = root / spec["file"]
    lines = gzip.decompress(path.read_bytes()).decode().splitlines()
    path.write_bytes(gzip.compress(("\n".join(transform(lines)) + "\n").encode()))


@pytest.mark.parametrize(
    ("transform", "reasons"),
    [
        # A renamed column is both missing and unexpected.
        (
            lambda ls: [ls[0].replace("icd_code", "icd")] + ls[1:],
            ["SCHEMA.MISSING_COLUMN:icd_code", "SCHEMA.UNEXPECTED_COLUMN:icd"],
        ),
        (
            lambda ls: [ls[0] + ",extra"] + [line + "," for line in ls[1:]],
            ["SCHEMA.UNEXPECTED_COLUMN:extra"],
        ),
        (lambda ls: ls + [ls[1]], ["KEY.DUPLICATE"]),
        (
            lambda ls: ls[:1] + [ls[1].replace(",9", ",nine", 1)],
            ["VALUE.TYPE_MISMATCH:InvalidTextRepresentation"],
        ),
    ],
)
def test_contract_quarantines_bad_tables(tmp_path, transform, reasons):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    import psycopg

    root = make_relational.write(tmp_path)
    _with_header(root, "diagnoses_icd", transform)
    with psycopg.connect(DSN) as conn:
        result = relational.load_table(conn, make_relational.CONTRACT, "diagnoses_icd", root)
        assert (result.status, result.reason_codes) == ("quarantined", reasons)
        with conn.cursor() as cur:
            cur.execute("select to_regclass('mimic.diagnoses_icd')")
            assert cur.fetchone()[0] is None  # a failed table is not left behind
        with conn.cursor() as cur:
            cur.execute("drop schema if exists mimic cascade")
