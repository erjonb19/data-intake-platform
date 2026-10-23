"""SFTP channel (Stage 2): which files a sync pass takes, where they land, and how the
intake treats them. A fake SFTP client serves a local folder; files are invented."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from dip import BUCKET_LANDING, CHANNEL_KEY
from dip.contracts import Contract, load_contracts
from dip.intake import process_landing
from dip.sftp_sync import sync_account
from dip.storage import LocalStore
from tests.unit.test_intake import FakeWarehouse

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eligibility"
FEED = "healthcare.eligibility"


class FakeSFTP:
    """paramiko.SFTPClient's listdir_attr, open and remove, over a local chroot."""

    def __init__(self, root: Path):
        self.root = root

    def _p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def listdir_attr(self, path: str):
        p = self._p(path)
        if not p.is_dir():
            raise FileNotFoundError(path)
        out = []
        for child in sorted(p.iterdir()):
            st = child.stat()
            out.append(
                SimpleNamespace(
                    filename=child.name,
                    st_mode=st.st_mode,
                    st_mtime=st.st_mtime,
                    st_size=st.st_size,
                )
            )
        return out

    def open(self, path: str, mode: str = "rb"):
        return self._p(path).open(mode)

    def remove(self, path: str) -> None:
        self._p(path).unlink()


def upload(root: Path, feed: str, name: str, data: bytes, age_seconds: float = 3600) -> Path:
    p = root / "upload" / feed / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    t = NOW.timestamp() - age_seconds
    os.utime(p, (t, t))
    return p


@pytest.fixture
def contracts():
    c = load_contracts()
    elig = c[FEED]
    source = {**elig.spec["source"], "channels": ["minio", "sftp"]}
    c[FEED] = Contract(elig.feed, elig.version, {**elig.spec, "source": source}, elig.path)
    return c


def test_complete_files_move_to_landing_with_their_channel(tmp_path, contracts):
    server, store = tmp_path / "srv", LocalStore(tmp_path / "store")
    good = (FIXTURES / "good/elig_good.csv").read_bytes()
    upload(server, FEED, "elig_20261022.csv", good)  # quiet for an hour
    upload(server, FEED, "elig_20261021.csv", good + b"\n", age_seconds=5)
    upload(server, FEED, "elig_20261021.csv.done", b"", age_seconds=5)  # marked complete
    upload(server, FEED, "elig_20261020.csv", good, age_seconds=5)  # still being written
    upload(server, FEED, "elig_20261019.csv.part", good)
    upload(server, FEED, ".hidden", good)

    r = sync_account(FakeSFTP(server), "healthcare", store, contracts, NOW)

    assert sorted(k for _, k in r.moved) == [
        f"{FEED}/elig_20261021.csv",
        f"{FEED}/elig_20261022.csv",
    ]
    assert r.waiting == [f"/upload/{FEED}/elig_20261020.csv"]
    assert r.feeds == [FEED]
    assert store.metadata(BUCKET_LANDING, f"{FEED}/elig_20261022.csv") == {
        CHANNEL_KEY: "sftp",
        "dip-sftp-user": "healthcare",
    }
    left = sorted(p.name for p in (server / "upload" / FEED).iterdir())
    assert left == [".hidden", "elig_20261019.csv.part", "elig_20261020.csv"]


def test_other_packs_feeds_and_unknown_folders_are_refused(tmp_path, contracts):
    server, store = tmp_path / "srv", LocalStore(tmp_path / "store")
    upload(server, FEED, "elig_20261022.csv", b"x")
    upload(server, "acme.unknown", "a.csv", b"x")
    (server / "upload" / "stray.csv").write_bytes(b"x")

    r = sync_account(FakeSFTP(server), "finance", store, contracts, NOW)

    assert r.moved == []
    assert [path for path, _ in r.refused] == [
        "/upload/acme.unknown",
        f"/upload/{FEED}",
        "/upload/stray.csv",
    ]
    assert store.list(BUCKET_LANDING) == []


def test_an_unprocessed_landing_file_is_not_overwritten(tmp_path, contracts):
    server, store = tmp_path / "srv", LocalStore(tmp_path / "store")
    store.put(BUCKET_LANDING, f"{FEED}/elig_20261022.csv", b"earlier")
    upload(server, FEED, "elig_20261022.csv", b"later")

    r = sync_account(FakeSFTP(server), "healthcare", store, contracts, NOW)

    assert r.moved == [] and r.waiting == [f"/upload/{FEED}/elig_20261022.csv"]
    assert store.get(BUCKET_LANDING, f"{FEED}/elig_20261022.csv") == b"earlier"


def test_no_upload_folder_is_an_empty_pass(tmp_path, contracts):
    r = sync_account(FakeSFTP(tmp_path), "healthcare", LocalStore(tmp_path / "s"), contracts, NOW)
    assert (r.moved, r.waiting, r.refused) == ([], [], [])


def test_intake_records_the_channel_and_refuses_a_channel_not_in_the_contract(tmp_path, contracts):
    store, wh = LocalStore(tmp_path), FakeWarehouse()
    good = (FIXTURES / "good/elig_good.csv").read_bytes()
    store.put(BUCKET_LANDING, f"{FEED}/elig_20261021.csv", good, metadata={CHANNEL_KEY: "sftp"})
    store.put(
        BUCKET_LANDING, f"{FEED}/elig_20261020.csv", good + b"\n", metadata={CHANNEL_KEY: "api"}
    )
    out = {o.source_key.split("/")[-1]: o for o in process_landing(store, wh, contracts, NOW)}

    assert out["elig_20261021.csv"].status == "loaded"
    assert out["elig_20261020.csv"].reason_codes == ("FILE.CHANNEL:api",)
    assert [i.channel for i in wh.intake] == ["sftp"]


def test_a_feed_without_the_sftp_channel_is_refused_at_the_folder(tmp_path, contracts):
    """Review finding: moving it would only quarantine it later, and trigger a DAG that
    does not exist (FHIR has its own)."""
    server, store = tmp_path / "srv", LocalStore(tmp_path / "store")
    upload(server, "healthcare.fhir_patient_bundle", "p.json", b"{}")
    r = sync_account(FakeSFTP(server), "healthcare", store, contracts, NOW)
    assert (
        r.moved == []
        and r.refused[0][1] == "'healthcare.fhir_patient_bundle' does not accept the sftp channel"
    )
    assert (server / "upload/healthcare.fhir_patient_bundle/p.json").exists()


def test_filepart_is_never_taken_and_an_orphan_marker_is_removed(tmp_path, contracts):
    server, store = tmp_path / "srv", LocalStore(tmp_path / "store")
    upload(server, FEED, "elig_20261022.csv.filepart", b"partial")
    upload(server, FEED, "elig_20261021.csv.done", b"")  # its file was taken earlier
    r = sync_account(FakeSFTP(server), "healthcare", store, contracts, NOW)
    assert r.moved == []
    assert sorted(p.name for p in (server / "upload" / FEED).iterdir()) == [
        "elig_20261022.csv.filepart"
    ]
