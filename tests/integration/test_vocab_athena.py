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
