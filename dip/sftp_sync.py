"""SFTP channel (Stage 2): move complete files from vendors' SFTP drop folders to landing.

Each vendor has one SFTP account, named after its pack (`healthcare`, `finance`), chrooted
to its own home. It uploads into `/upload/<feed>/`, for example
`/upload/healthcare.eligibility/elig_20261022.csv`. A sync pass (the `sftp_sync` DAG):

1. lists `/upload/<feed>/` for each feed the account may send: the feed must be in the
   account's pack. Other folders are left alone and reported;
2. takes only **complete** files. A file is complete when the vendor has uploaded a
   `<name>.done` marker next to it, or when it has not changed for `quiet_seconds`
   (default 120; uploads in progress keep moving their modification time). Hidden files,
   `.part`/`.tmp`/`.filepart` names and markers themselves are never taken, and a marker
   left without its file is removed. A feed whose contract does not list `sftp` in
   `source.channels` is refused at the folder, not moved;
3. copies each complete file to `landing/<feed>/<name>` with the object metadata
   `dip-channel: sftp` and `dip-sftp-user: <account>`, then deletes it (and its marker)
   from the server. If landing still holds an unprocessed file of that name, the file
   waits for the next pass rather than overwrite it.

The intake then treats it like any other landed file (and would still quarantine it with
`FILE.CHANNEL:sftp` if the contract changed in between).

`client` is anything with paramiko's SFTPClient methods used here (`listdir_attr`, `open`,
`remove`), so tests use a local fake and the DAG a real connection.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dip import BUCKET_LANDING, CHANNEL_KEY
from dip.contracts import Contract
from dip.storage import ObjectStore

UPLOAD_ROOT = "/upload"
DONE = ".done"
NEVER_TAKEN = (".part", ".tmp", ".filepart", DONE)  # .filepart: WinSCP


@dataclass
class SyncResult:
    moved: list[tuple[str, str]] = field(default_factory=list)  # (feed, landing key)
    waiting: list[str] = field(default_factory=list)  # remote paths not complete yet
    refused: list[tuple[str, str]] = field(default_factory=list)  # (remote path, why)

    @property
    def feeds(self) -> list[str]:
        return sorted({feed for feed, _ in self.moved})


def _is_dir(attr: Any) -> bool:
    return stat.S_ISDIR(attr.st_mode or 0)


def sync_account(
    client: Any,
    account: str,
    store: ObjectStore,
    contracts: dict[str, Contract],
    now: datetime,
    quiet_seconds: int = 120,
) -> SyncResult:
    result = SyncResult()
    try:
        folders = client.listdir_attr(UPLOAD_ROOT)
    except FileNotFoundError:
        return result
    for folder in sorted(folders, key=lambda a: a.filename):
        feed = folder.filename
        path = f"{UPLOAD_ROOT}/{feed}"
        if not _is_dir(folder):
            result.refused.append((path, "not a folder: files go in /upload/<feed>/"))
            continue
        if feed not in contracts:
            result.refused.append((path, f"no contract for feed {feed!r}"))
            continue
        if feed.split(".")[0] != account:
            result.refused.append((path, f"account {account!r} may not send {feed!r}"))
            continue
        if "sftp" not in contracts[feed].spec["source"]["channels"]:
            result.refused.append((path, f"{feed!r} does not accept the sftp channel"))
            continue
        entries = {a.filename: a for a in client.listdir_attr(path) if not _is_dir(a)}
        for name in sorted(entries):
            # A marker whose file is gone (taken on a quiet period earlier) is dropped.
            if name.endswith(DONE) and name[: -len(DONE)] not in entries:
                client.remove(f"{path}/{name}")
                continue
            attr = entries[name]
            remote = f"{path}/{name}"
            if name.startswith(".") or name.endswith(NEVER_TAKEN):
                continue
            marked = f"{name}{DONE}" in entries
            age = now.timestamp() - (attr.st_mtime or 0)
            if not marked and age < quiet_seconds:
                result.waiting.append(remote)
                continue
            key = f"{feed}/{name}"
            if store.exists(BUCKET_LANDING, key):
                result.waiting.append(remote)
                continue
            with client.open(remote, "rb") as fh:
                data = fh.read()
            store.put(
                BUCKET_LANDING, key, data, metadata={CHANNEL_KEY: "sftp", "dip-sftp-user": account}
            )
            client.remove(remote)
            if marked:
                client.remove(f"{remote}{DONE}")
            result.moved.append((feed, key))
    return result
