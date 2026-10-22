"""Object storage: MinIO/S3 in the cluster, a local directory for tests and laptops.

Objects can carry a few string metadata entries (S3 user metadata). The intake channels
use one: `dip-channel` says how a landed file arrived (`sftp`, `api`; none means it was put
into MinIO directly)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class ObjectStore(Protocol):
    def get(self, bucket: str, key: str) -> bytes: ...
    def put(
        self, bucket: str, key: str, data: bytes, metadata: dict[str, str] | None = None
    ) -> None: ...
    def metadata(self, bucket: str, key: str) -> dict[str, str]: ...
    def exists(self, bucket: str, key: str) -> bool: ...
    def list(self, bucket: str, prefix: str = "") -> list[str]: ...
    def delete(self, bucket: str, key: str) -> None: ...


class LocalStore:
    """Buckets are subdirectories of `root`; keys are relative paths with `/`. Metadata
    lives beside the buckets, in `root/.meta/<bucket>/<key>.json`, so listings never see it."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, bucket: str, key: str) -> Path:
        p = (self.root / bucket / key).resolve()
        if not p.is_relative_to((self.root / bucket).resolve()):
            raise ValueError(f"key escapes bucket: {key!r}")
        return p

    def get(self, bucket: str, key: str) -> bytes:
        return self._path(bucket, key).read_bytes()

    def _meta_path(self, bucket: str, key: str) -> Path:
        self._path(bucket, key)  # the same escape check
        return self.root / ".meta" / bucket / f"{key}.json"

    def put(
        self, bucket: str, key: str, data: bytes, metadata: dict[str, str] | None = None
    ) -> None:
        p = self._path(bucket, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        m = self._meta_path(bucket, key)
        if metadata:
            m.parent.mkdir(parents=True, exist_ok=True)
            m.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
        else:
            m.unlink(missing_ok=True)

    def metadata(self, bucket: str, key: str) -> dict[str, str]:
        m = self._meta_path(bucket, key)
        return json.loads(m.read_text(encoding="utf-8")) if m.is_file() else {}

    def exists(self, bucket: str, key: str) -> bool:
        return self._path(bucket, key).is_file()

    def list(self, bucket: str, prefix: str = "") -> list[str]:
        base = self.root / bucket
        if not base.is_dir():
            return []
        keys = (p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
        return sorted(k for k in keys if k.startswith(prefix))

    def delete(self, bucket: str, key: str) -> None:
        self._path(bucket, key).unlink(missing_ok=True)
        self._meta_path(bucket, key).unlink(missing_ok=True)


class S3Store:
    """Wraps a boto3 S3 client (in Airflow: `S3Hook(aws_conn_id="minio").get_conn()`)."""

    def __init__(self, client: Any):
        self.client = client

    def get(self, bucket: str, key: str) -> bytes:
        return self.client.get_object(Bucket=bucket, Key=key)["Body"].read()

    def put(
        self, bucket: str, key: str, data: bytes, metadata: dict[str, str] | None = None
    ) -> None:
        self.client.put_object(Bucket=bucket, Key=key, Body=data, Metadata=metadata or {})

    def metadata(self, bucket: str, key: str) -> dict[str, str]:
        return dict(self.client.head_object(Bucket=bucket, Key=key).get("Metadata", {}))

    def exists(self, bucket: str, key: str) -> bool:
        resp = self.client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
        return any(o["Key"] == key for o in resp.get("Contents", []))

    def list(self, bucket: str, prefix: str = "") -> list[str]:
        keys: list[str] = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix
        ):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        return sorted(keys)

    def delete(self, bucket: str, key: str) -> None:
        self.client.delete_object(Bucket=bucket, Key=key)
