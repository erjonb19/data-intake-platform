"""R7a: the cohort service's read-only role over the shareable tier, Synthea only.

Applies the real role file (infra/terraform/files/postgres-roles.sql), builds the fixture
patient through dbt, then connects as dip_cohort_reader. The role may read the shareable
tier only while the warehouse holds Synthea alone (classified live by the dbt on-run-end
hook grant_cohort_reader); a MIMIC or unknown source takes every grant away, and MIMIC
leaves a block row that only a person removes.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dip import BUCKET_LANDING, vocab
from dip.contracts import REPO_ROOT, load_contracts
from dip.dbt_runner import run_dbt
from dip.deid import install_key
from dip.intake import process_landing
from dip.storage import LocalStore
from tests.fixtures.fhir.make_fixtures import good_bundle
from tests.integration.conftest import DSN, rows, scalar

FEED = "healthcare.fhir_patient_bundle"
CACHE = Path(os.environ.get("DIP_CACHE_DIR", "data/cache"))
ROLES_SQL = REPO_ROOT / "infra/terraform/files/postgres-roles.sql"
# A fixed, public test password: fine for a throwaway CI database, never for a deployment.
READER_PASSWORD = "ci-cohort-reader-0123456789"


def reader_dsn() -> str:
    import psycopg

    info = psycopg.conninfo.conninfo_to_dict(DSN)
    info.update(user="dip_cohort_reader", password=READER_PASSWORD)
    return psycopg.conninfo.make_conninfo(**info)


@pytest.fixture(scope="module")
def tier(module_warehouse, tmp_path_factory):
    wh = module_warehouse
    with wh.conn.cursor() as cur:
        cur.execute(ROLES_SQL.read_text(encoding="utf-8"))
        cur.execute(f"alter role dip_cohort_reader password '{READER_PASSWORD}'")
    wh.conn.commit()
    store = LocalStore(tmp_path_factory.mktemp("store"))
    store.put(BUCKET_LANDING, f"{FEED}/fixture.json", json.dumps(good_bundle()).encode())
    process_landing(store, wh, load_contracts(), datetime.now(UTC))
    vocab.load(wh.conn, vocab.fetch(CACHE))
    install_key(wh.conn, "ci-test-key-0123456789abcdef0123456789abcdef")
    assert run_dbt(DSN, "build", check=False).returncode == 0
    return wh


def reader_can_read(table: str) -> bool:
    import psycopg

    with psycopg.connect(reader_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        try:
            cur.execute(f"select 1 from {table} limit 1")
        except psycopg.errors.InsufficientPrivilege:
            return False
        return True


def rebuild(selector: str = "shareable") -> None:
    # Any dbt run ends with the on-run-end hooks, including grant_cohort_reader.
    assert run_dbt(DSN, "run", "--select", selector, check=False).returncode == 0


def add_feed(wh, feed: str) -> None:
    """A loaded file on another feed, next to the Synthea data (a copy of a real row)."""
    with wh.conn.cursor() as cur:
        cur.execute(
            "insert into ops.intake_file (file_sha256, feed, contract_version, source_key,"
            " raw_key, status, accepted_count, rejected_count, skipped_count, received_at)"
            " select md5(%s), %s, contract_version, 'test/' || %s, raw_key, status, 0, 0, 0,"
            " received_at from ops.intake_file where status <> 'quarantined' limit 1",
            (feed, feed, feed),
        )
    wh.conn.commit()


def remove_feed(wh, feed: str, unblock: bool = True) -> None:
    """Remove the file and, as the documented human step, the MIMIC block row."""
    with wh.conn.cursor() as cur:
        cur.execute("delete from ops.intake_file where feed = %s", (feed,))
        if unblock:
            cur.execute("drop table if exists ops.cohort_reader_block")
    wh.conn.commit()


def test_cdm_source_says_synthea(tier):
    assert rows(
        tier,
        "select cdm_source_abbreviation, cdm_version, cdm_version_concept_id,"
        " left(vocabulary_version, 8) from omop_shareable.cdm_source",
    ) == [("DIP-SYNTHEA", "v5.4", 756265, "Eunomia ")]


def test_reader_reads_the_shareable_tier(tier):
    import psycopg

    with psycopg.connect(reader_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        # search_path is omop_shareable, so the service can use bare OMOP table names.
        cur.execute("select count(*) from person")
        assert cur.fetchone()[0] == 1
        cur.execute("select count(*) from procedure_occurrence")
        assert cur.fetchone()[0] == 1
        cur.execute("select cdm_source_abbreviation from cdm_source")
        assert cur.fetchone()[0] == "DIP-SYNTHEA"
        cur.execute("show default_transaction_read_only")
        assert cur.fetchone()[0] == "on"
        cur.execute("show statement_timeout")
        assert cur.fetchone()[0] == "1min"


def test_reader_reads_nothing_else_and_writes_nothing(tier):
    import psycopg

    for table in (
        "omop_shareable.concept",  # a local Athena copy is licensed to its downloader
        "omop.person",
        "deid.deid_person_map",
        "ops_secret.deid_key",
        "ops.intake_file",
        "bronze.fhir_resource",
        "vocab.concept",
    ):
        assert not reader_can_read(table), table
    with psycopg.connect(reader_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        for statement in (
            "insert into omop_shareable.person (person_id) values (-1)",
            "delete from omop_shareable.person",
            "create table omop_shareable.scratch (x int)",
            "create table public.scratch (x int)",
        ):
            # Refused twice over: the session is read-only, and the role has no grant.
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                cur.execute(statement)
            cur.execute("begin read write")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(statement)
            cur.execute("rollback")


def test_reader_role_attributes(tier):
    assert rows(
        tier,
        "select rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolconnlimit,"
        " (select count(*) from pg_auth_members where member = r.oid)"
        " from pg_roles r where rolname = 'dip_cohort_reader'",
    ) == [(False, False, False, False, 5, 0)]


@pytest.mark.parametrize(
    ("feed", "expected"),
    [("mimic.fhir_demo", "DIP-MIMIC"), ("acme.claims", "DIP-UNKNOWN")],
)
def test_any_other_source_takes_every_grant_away(tier, feed, expected):
    """Fails closed: one MIMIC (or unclassified) file next to Synthea, and the reader can
    read nothing; remove it and the grants come back on the next build."""
    add_feed(tier, feed)
    try:
        rebuild()
        assert scalar(tier, "select cdm_source_abbreviation from omop_shareable.cdm_source") == (
            expected
        )
        for table in ("cdm_source", "person", "procedure_occurrence", "condition_occurrence"):
            assert not reader_can_read(f"omop_shareable.{table}"), table
        assert not scalar(
            tier, "select has_schema_privilege('dip_cohort_reader', 'omop_shareable', 'USAGE')"
        )
    finally:
        remove_feed(tier, feed)
    rebuild()
    assert reader_can_read("omop_shareable.person")


def test_a_partial_run_cannot_grant_through_a_stale_cdm_source(tier):
    """Review finding: MIMIC arrives, and a run rebuilds share_person but not cdm_source,
    which still says DIP-SYNTHEA. The hook classifies live, so it revokes instead."""
    assert reader_can_read("omop_shareable.person")
    add_feed(tier, "mimic.fhir_demo")
    try:
        rebuild("share_person")
        assert scalar(tier, "select cdm_source_abbreviation from omop_shareable.cdm_source") == (
            "DIP-SYNTHEA"  # stale on purpose: not rebuilt
        )
        assert not reader_can_read("omop_shareable.person")
        assert not reader_can_read("omop_shareable.cdm_source")
    finally:
        remove_feed(tier, "mimic.fhir_demo")
    rebuild()


def test_grants_only_what_the_run_rebuilt(tier):
    """Synthea-only, but a run that rebuilt one table grants that table; a table that this
    run did not rebuild keeps what it had, and the hook never names a stray table."""
    with tier.conn.cursor() as cur:
        cur.execute("create table omop_shareable.stray_copy as select * from omop_shareable.person")
    tier.conn.commit()
    try:
        rebuild()
        assert not reader_can_read("omop_shareable.stray_copy")
        assert reader_can_read("omop_shareable.person")
        rebuild("share_person")
        assert reader_can_read("omop_shareable.person")
        assert reader_can_read("omop_shareable.measurement")  # not rebuilt, kept
    finally:
        with tier.conn.cursor() as cur:
            cur.execute("drop table omop_shareable.stray_copy")
        tier.conn.commit()


def test_run_operation_alone_only_revokes(tier):
    """Without run results the hook grants nothing; with Synthea alone it changes nothing."""
    assert run_dbt(DSN, "run-operation", "grant_cohort_reader", check=False).returncode == 0
    assert reader_can_read("omop_shareable.person")
    add_feed(tier, "acme.claims")
    try:
        assert run_dbt(DSN, "run-operation", "grant_cohort_reader", check=False).returncode == 0
        assert not reader_can_read("omop_shareable.person")
    finally:
        remove_feed(tier, "acme.claims")
    rebuild()


def test_mimic_blocks_until_a_person_clears_it(tier):
    """Second review: classification by what is loaded now would forget MIMIC whose rows
    were later deleted (or quarantined on a reload) while OMOP still holds what they fed.
    The block row keeps the warehouse DIP-MIMIC until a person removes it."""
    add_feed(tier, "mimic.fhir_demo")
    rebuild()
    remove_feed(tier, "mimic.fhir_demo", unblock=False)
    rebuild()
    assert scalar(tier, "select cdm_source_abbreviation from omop_shareable.cdm_source") == (
        "DIP-MIMIC"
    )
    assert not reader_can_read("omop_shareable.person")
    assert scalar(tier, "select count(*) from ops.cohort_reader_block") == 1
    remove_feed(tier, "mimic.fhir_demo")  # the person, after rebuilding from Synthea alone
    rebuild()
    assert reader_can_read("omop_shareable.person")


def test_a_quarantined_mimic_file_still_counts(tier):
    add_feed(tier, "mimic.fhir_demo")
    with tier.conn.cursor() as cur:
        cur.execute(
            "update ops.intake_file set status = 'quarantined' where feed = 'mimic.fhir_demo'"
        )
    tier.conn.commit()
    try:
        rebuild()
        assert not reader_can_read("omop_shareable.person")
    finally:
        remove_feed(tier, "mimic.fhir_demo")
    rebuild()
