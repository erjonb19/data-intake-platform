"""Object storage: MinIO/S3 in the cluster, a local directory for tests and laptops."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class ObjectStore(Protocol):
    def get(self, bucket: str, key: str) -> bytes: ...
    def put(self, bucket: str, key: str, data: bytes) -> None: ...
    def exists(self, bucket: str, key: str) -> bool: ...
    def list(self, bucket: str, prefix: str = "") -> list[str]: ...
    def delete(self, bucket: str, key: str) -> None: ...


class LocalStore:
    """Buckets are subdirectories of `root`; keys are relative paths with `/`."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, bucket: str, key: str) -> Path:
        p = (self.root / bucket / key).resolve()
        if not p.is_relative_to((self.root / bucket).resolve()):
            raise ValueError(f"key escapes bucket: {key!r}")
        return p

    def get(self, bucket: str, key: str) -> bytes:
        return self._path(bucket, key).read_bytes()

    def put(self, bucket: str, key: str, data: bytes) -> None:
        p = self._path(bucket, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

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


class S3Store:
    """Wraps a boto3 S3 client (in Airflow: `S3Hook(aws_conn_id="minio").get_conn()`)."""

    def __init__(self, client: Any):
        self.client = client

    def get(self, bucket: str, key: str) -> bytes:
        return self.client.get_object(Bucket=bucket, Key=key)["Body"].read()

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=bucket, Key=key, Body=data)

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
