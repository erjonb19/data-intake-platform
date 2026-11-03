"""Stage 11: the check run before a runner's files are committed (scripts/check_recordings.py):
the job's own key, unredacted auth headers and key-shaped strings fail it; redacted
headers and the repository's real recordings pass."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_recordings",
                                              ROOT / "scripts" / "check_recordings.py")  # fmt: skip
check_recordings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_recordings)


def run(tmp_path, text, key_env=(), env=None, monkeypatch=None):
    (tmp_path / "r.json").write_text(text, "utf-8")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    args = [a for k in key_env for a in ("--key-env", k)]
    return check_recordings.main([*args, str(tmp_path)])


def test_the_jobs_own_key_fails(tmp_path, monkeypatch):
    assert run(tmp_path, '{"x": "a zz-not-shaped-zz b"}', ["K"], {"K": "zz-not-shaped-zz"},
               monkeypatch) == 1  # fmt: skip


def test_a_key_that_is_not_set_fails_rather_than_passing(tmp_path, monkeypatch):
    monkeypatch.delenv("K", raising=False)
    assert run(tmp_path, "{}", ["K"]) == 1


def test_auth_headers_must_be_redacted(tmp_path):
    assert run(tmp_path, '{"x-api-key": "abcdef123456"}') == 1
    assert run(tmp_path, "Authorization: Bearer abc.def") == 1
    assert run(tmp_path, '{"x-api-key": "[redacted]", "authorization": "[redacted]"}') == 0


def test_key_shapes_fail_without_the_key(tmp_path):
    assert run(tmp_path, '"sk-ant-api03-' + "a" * 30 + '"') == 1
    assert run(tmp_path, '"AIza' + "b" * 35 + '"') == 1


def test_the_committed_recordings_pass():
    assert check_recordings.main([str(ROOT / "agents" / "evals")]) == 0
