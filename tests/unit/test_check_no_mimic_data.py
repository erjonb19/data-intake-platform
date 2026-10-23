"""scripts/check_no_mimic_data.sh, run in a throwaway git repository."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dip.contracts import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "check_no_mimic_data.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH or not shutil.which("git"), reason="needs bash and git")

# Built from pieces so this file does not itself match the patterns it tests.
MIMIC = "http://mimic.mit.edu/fhir/mimic/"
MIMIC_PATIENT = (
    '{"resourceType": "Patient", "identifier": [{"system": "'
    + MIMIC
    + 'identifier/patient"'
    + ', "value": "10000032"}]}'
)
MIMIC_PROFILE = '{"meta": {"profile": ["' + MIMIC + "Structure" + 'Definition/mimic-patient"]}}'
LOCAL_ONLY = '{"name": "x", "dip_distribution"' + ': "local-only"}'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    (tmp_path / "README.md").write_text("see http://mimic.mit.edu/fhir/mimic\n")
    _git(tmp_path, "add", ".")
    return tmp_path


def _check(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "scripts/check_no_mimic_data.sh", *args], cwd=repo, capture_output=True, text=True
    )


def _add(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", path)


def test_clean_repo_passes(repo):
    _add(repo, "tests/fixtures/x/rows.csv", "a,b\n1,2\n")
    _add(repo, "dbt/seeds/map.csv", "a,b\n")
    _add(repo, "dip/contract.schema.json", "{}")
    _add(repo, "agents/evals/triage/recordings/missing_column.json", "[]")
    _add(repo, "observability/grafana/feeds.json", "{}")
    _add(repo, "agents/evals/triage/results/variance.json", "{}")
    assert _check(repo).returncode == 0
    assert _check(repo, "--staged").returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        "out/cohort.csv",
        "extracts/omop/person.parquet",
        "warehouse.duckdb",
        "packs/mimic/datapackage.json",
        "dbt/seeds/nested/map.csv",
        "agents/evals/triage/recordings/nested/case.json",
        "agents/evals/triage/results.json",
        "agents/evals/triage/results/nested/run.json",
        "observability/grafana/exports/feeds.json",
        "observability/feeds.json",
        "mimic-iv-demo/hosp/patients.txt",
        "bundle/Patient.ndjson",
    ],
)
def test_data_files_outside_reviewed_paths_fail(repo, path):
    _add(repo, path, "a\n")
    for args in ((), ("--staged",)):
        result = _check(repo, *args)
        assert result.returncode == 1, args
        assert path in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        MIMIC_PATIENT,
        MIMIC_PROFILE,
        LOCAL_ONLY,
    ],
)
def test_mimic_content_fails_even_in_fixtures(repo, text):
    _add(repo, "tests/fixtures/real/patient.txt", text)
    result = _check(repo)
    assert result.returncode == 1
    assert "tests/fixtures/real/patient.txt" in result.stderr


def test_staged_mode_reports_only_this_commits_files(repo):
    _add(repo, "notes.txt", LOCAL_ONLY)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "x")
    _add(repo, "other.txt", "fine\n")
    assert _check(repo, "--staged").returncode == 0
    assert _check(repo).returncode == 1


@pytest.mark.parametrize(
    "path",
    [
        "dbt/seeds/CONCEPT.csv",
        "tests/fixtures/vocab/CONCEPT_CPT4.csv",
        "tests/fixtures/vocab/CONCEPT_CLASS.csv",
        "tests/fixtures/vocab/CONCEPT_RELATIONSHIP.csv",
        "dbt/seeds/DOMAIN.csv",
        "tests/fixtures/RELATIONSHIP.csv",
        "tests/fixtures/VOCABULARY.csv",
        "tests/fixtures/DRUG_STRENGTH.csv",
        "tests/fixtures/d_labitems_to_loinc.csv",
    ],
)
def test_athena_files_fail_anywhere(repo, path):
    _add(repo, path, "concept_id\n")
    result = _check(repo)
    assert result.returncode == 1
    assert path in result.stderr
