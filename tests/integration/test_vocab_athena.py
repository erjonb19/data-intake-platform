"""Loading a full Athena download, using two tiny invented folders in Athena's format.

Real Athena files are licensed to whoever downloaded them, so none are in the repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dip import vocab
from tests.integration.conftest import rows, scalar

CONCEPT_HEADER = "\t".join(vocab.CONCEPT_COLUMNS)
REL_HEADER = "\t".join(vocab.RELATIONSHIP_COLUMNS)


def _folder(root: Path, name: str, concepts: list[str], rels: list[str], release: str) -> Path:
    d = root / name
    d.mkdir()
    (d / "CONCEPT.csv").write_text(
        "\n".join([CONCEPT_HEADER, *concepts]) + "\n", encoding="utf-8", newline=""
    )
    (d / "CONCEPT_RELATIONSHIP.csv").write_text(
        "\n".join([REL_HEADER, *rels]) + "\n", encoding="utf-8", newline=""
    )
    (d / "VOCABULARY.csv").write_text(
        "vocabulary_id\tvocabulary_name\tvocabulary_reference\tvocabulary_version"
        f"\tvocabulary_concept_id\nNone\tOMOP Standardized Vocabularies\tOMOP generated"
        f"\t{release}\t44819096\n{name}\t{name}\tinvented\t1\t0\n",
        encoding="utf-8",
        newline="",
    )
    return d


@pytest.fixture
def folders(tmp_path):
    a = _folder(
        tmp_path,
        "VocabA",
        [
            # A quote and a backslash in a name must load as-is (Athena does not quote).
            '900001\tSource "code" A\\1\tCondition\tVocabA\tCode\t\tA1\t20200101\t20991231\t',
            "900002\tStandard A\tCondition\tVocabA\tCode\tS\tA2\t20200101\t20991231\t",
        ],
        [
            "900001\t900002\tMaps to\t20200101\t20991231\t",
            "900001\t900002\tIs a\t20200101\t20991231\t",
        ],
        "v5.0 TEST",
    )
    b = _folder(
        tmp_path,
        "VocabB",
        [
            # Repeats 900002 (every Athena download repeats the base vocabularies).
            "900002\tStandard A (other copy)\tCondition\tVocabA\tCode\tS\tA2\t20200101\t20991231\t",
            "900003\tSource B\tCondition\tVocabB\tCode\t\tB1\t20200101\t20991231\t",
        ],
        [
            "900001\t900002\tMaps to\t20200101\t20991231\t",
            "900003\t900002\tMaps to\t20200101\t20991231\t",
        ],
        "v5.0 TEST",
    )
    return a, b


def test_load_athena_dedupes_keeps_maps_to_and_records_release(warehouse, folders):
    concepts, maps_to = vocab.load_athena(warehouse.conn, list(folders))
    assert (concepts, maps_to) == (3, 2)
    assert rows(warehouse, "select concept_id, concept_name from vocab.concept order by 1") == [
        (900001, 'Source "code" A\\1'),
        (900002, "Standard A"),  # the first folder wins
        (900003, "Source B"),
    ]
    assert scalar(warehouse, "select count(*) from vocab.concept_relationship") == 2
    assert vocab.provenance(warehouse.conn) == {
        "name": "OHDSI Athena",
        "release": "v5.0 TEST",
        "vocabularies": ["None", "VocabA", "VocabB"],
    }


def test_load_configured_skips_an_unchanged_athena_release(warehouse, folders, monkeypatch):
    import os

    monkeypatch.setenv("DIP_ATHENA_DIRS", os.pathsep.join(str(f) for f in folders))
    vocab.load_configured(warehouse.conn)
    with warehouse.conn.cursor() as cur:
        cur.execute("insert into vocab.concept_relationship values (1, 2, 'Marker', now(), now())")
    warehouse.conn.commit()
    vocab.load_configured(warehouse.conn)  # same release: not reloaded
    assert scalar(warehouse, "select count(*) from vocab.concept_relationship") == 3


def test_header_mismatch_is_refused(warehouse, tmp_path):
    d = _folder(tmp_path, "Bad", [], [], "v")
    (d / "CONCEPT.csv").write_text("id\tname\n1\tx\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected header"):
        vocab.load_athena(warehouse.conn, [d])


def test_eunomia_provenance_unchanged(warehouse):
    vocab.load(warehouse.conn, vocab.fetch(vocab.CACHE_DIR))
    assert vocab.provenance(warehouse.conn) == vocab.EUNOMIA_PROVENANCE


def test_warehouse_init_creates_the_map_for_an_older_vocabulary(warehouse):
    # A warehouse whose vocabulary was loaded before source_to_concept_map existed is
    # never reloaded (needs_load is false), but every dbt build reads the map.
    vocab.load(warehouse.conn, vocab.fetch(vocab.CACHE_DIR))
    with warehouse.conn.cursor() as cur:
        cur.execute("drop table vocab.source_to_concept_map")
    warehouse.conn.commit()
    assert vocab.needs_load(warehouse.conn) is False
    warehouse.init()
    assert scalar(warehouse, "select to_regclass('vocab.source_to_concept_map') is not null")


def test_mapping_gaps_only_checks_mapped_source_vocabularies(warehouse, tmp_path):
    rows_ = [f"{900100 + i}\tSource {i}\tCondition\tICD10CM\tCode\t\tX{i}\t20200101\t20991231\t"
             for i in range(3)]  # fmt: skip
    rows_ += [f"{900200 + i}\tTerm {i}\tObservation\tMeSH\tCode\t\tM{i}\t20200101\t20991231\t"
              for i in range(3)]  # fmt: skip
    d = _folder(tmp_path, "Gaps", rows_, [], "v")
    vocab.load_athena(warehouse.conn, [d])
    # ICD10CM without mappings is reported (it is what MIMIC diagnoses map from); MeSH,
    # which never maps, is not.
    assert vocab.mapping_gaps(warehouse.conn) == {
        "ICD10CM": {"non_standard": 3, "without_maps_to": 3}
    }


def test_two_maps_to_targets_resolve_to_the_lowest_concept(warehouse, tmp_path):
    """A code with two standard targets (a vocabulary code or a site-map row) always maps to
    the lowest standard concept id, whatever order the rows were loaded in."""
    from dip.dbt_runner import run_dbt
    from tests.integration.conftest import DSN

    concepts = [
        "900402\tTarget high\tCondition\tVocabT\tCode\tS\tT2\t20200101\t20991231\t",
        "900401\tTarget low\tMeasurement\tVocabT\tCode\tS\tT1\t20200101\t20991231\t",
        "900400\tSource\tCondition\tICD10CM\tCode\t\tQ1\t20200101\t20991231\t",
    ]
    rels = [
        "900400\t900402\tMaps to\t20200101\t20991231\t",
        "900400\t900401\tMaps to\t20200101\t20991231\t",
    ]
    vocab.load_athena(warehouse.conn, [_folder(tmp_path, "VocabT", concepts, rels, "v")])
    site_map = [
        ("L1", 0, "TEST_SITE", "Local", target, "VocabT", "1970-01-01", "2099-12-31", None)
        for target in (900402, 900401)
    ]
    vocab.load_source_to_concept_map(warehouse.conn, "TEST_SITE", site_map)
    assert run_dbt(DSN, "run", "--select", "source_to_standard", check=False).returncode == 0
    assert rows(
        warehouse,
        "select vocabulary_id, concept_code, standard_concept_id, standard_domain_id"
        " from silver.source_to_standard where concept_code in ('Q1', 'L1') order by 1",
    ) == [("ICD10CM", "Q1", 900401, "Measurement"), ("TEST_SITE", "L1", 900401, "Measurement")]
