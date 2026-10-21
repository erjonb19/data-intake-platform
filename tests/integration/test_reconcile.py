"""M3: reconcile OMOP built from the two synthetic MIMIC-shaped sources.

Side a: the FHIR-shaped export through the FHIR intake (database DIP_TEST_DSN).
Side b: the same invented patient as relational tables (a second database, <name>_rel).
The fixtures differ on purpose: an ED visit only in FHIR (explained) and one lab result
only in the relational tables (unexplained), and the reconciliation must say so.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from dip import BUCKET_LANDING, relational, vocab
from dip.contracts import REPO_ROOT, load_contracts
from dip.dbt_runner import run_dbt
from dip.deid import install_key
from dip.fhir_bulk import bundle_export
from dip.intake import process_landing
from dip.storage import LocalStore
from dip.warehouse import Warehouse
from tests.fixtures.mimic_shaped import make_fixture, make_relational
from tests.integration.conftest import DSN, OWNED_SCHEMAS, _fresh_warehouse

CACHE = Path(os.environ.get("DIP_CACHE_DIR", "data/cache"))
KEY = "ci-test-key-0123456789abcdef0123456789abcdef"


def _rel_dsn() -> str:
    u = urlsplit(DSN)
    return urlunsplit(u._replace(path=u.path + "_rel"))


@pytest.fixture(scope="module")
def sides(tmp_path_factory):
    if not DSN:
        pytest.skip("DIP_TEST_DSN not set")
    import psycopg

    root = tmp_path_factory.mktemp("reconcile")
    # Side a: FHIR-shaped export.
    a = _fresh_warehouse()
    store = LocalStore(root / "store")
    for pid, data in bundle_export(make_fixture.write(root / "export"), root / "spool"):
        store.put(BUCKET_LANDING, f"mimic.fhir_demo/{pid}.json", data)
    contracts = load_contracts((REPO_ROOT / "packs/mimic/contracts",))
    process_landing(store, a, contracts, datetime(2026, 10, 21, tzinfo=UTC))
    vocab.load(a.conn, vocab.fetch(CACHE))
    install_key(a.conn, KEY)
    for cmd in ("seed", "run"):
        assert run_dbt(DSN, cmd, check=False).returncode == 0

    # Side b: relational tables, in their own database.
    rel = _rel_dsn()
    with psycopg.connect(DSN, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute("select 1 from pg_database where datname = %s", (urlsplit(rel).path[1:],))
        if not cur.fetchone():
            cur.execute(f"create database {urlsplit(rel).path[1:]}")
    b_conn = psycopg.connect(rel)
    with b_conn.cursor() as cur:
        for schema in (*OWNED_SCHEMAS, "mimic"):
            cur.execute(f"drop schema if exists {schema} cascade")
    b_conn.commit()
    b = Warehouse(b_conn)
    b.init()
    results = relational.load_all(
        b.conn, make_relational.CONTRACT, make_relational.write(root / "rel")
    )
    assert {r.status for r in results} == {"loaded"}
    vocab.load(b.conn, vocab.fetch(CACHE))
    install_key(b.conn, KEY)
    for cmd in ("seed", "run"):
        args = [cmd, "--vars", "{source_kind: mimic_relational}"]
        assert run_dbt(rel, *args, check=False).returncode == 0
    yield a.conn, b.conn
    a.conn.close()
    b.conn.close()


def test_reconciliation_finds_and_explains_the_differences(sides):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_reconcile", REPO_ROOT / "packs/mimic/run_reconcile.py"
    )
    run_reconcile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_reconcile)

    report = run_reconcile.run(*sides)
    assert report["person"]["matched"] == 1
    assert report["visit"]["matched"] == 1
    assert list(report["visit"]["only_fhir"].values()) == [1]
    assert "ED visit" in next(iter(report["visit"]["only_fhir"]))
    assert report["condition"]["matched"] == 2
    # The ICU lab folds into the hospital visit on the FHIR side and matches exactly,
    # time zone conversion included.
    assert report["measurement"]["matched"] == 1
    assert report["measurement"]["only_relational"] == {"unexplained": 1}
    assert report["unexplained"] == 1


@pytest.mark.parametrize("side", [0, 1], ids=["fhir", "relational"])
def test_mimic_derived_extract_is_odbl_with_attribution(sides, side):
    import json

    from dip import extract

    conn = sides[side]
    files = extract.build_files(conn, "0.0.1", git_commit="0" * 40)
    pkg = json.loads(files[extract.DESCRIPTOR])
    assert [lic["name"] for lic in pkg["licenses"]] == ["ODbL-1.0"]
    assert pkg["dip_distribution"] == "local-only"
    assert "REAL, de-identified" in pkg["description"]
    assert any("10.13026/dp1f-ex47" in s["path"] for s in pkg["sources"])
    assert pkg["provenance"]["source"].startswith("MIMIC-IV demo")
    with pytest.raises(extract.LicenseError):
        extract.build_files(conn, "0.0.1", "CC0-1.0", git_commit="0" * 40)
