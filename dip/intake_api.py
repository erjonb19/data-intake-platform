"""Intake API (Stage 2): the `api` channel. Vendors POST a file; it lands in MinIO.

    POST /v1/feeds/{feed}/files?filename=elig_20261022.csv     body: the file's bytes
    Authorization: Bearer <api key>

  202  {"feed", "key", "sha256", "bytes"}: the file is in landing/<feed>/<filename> with
       the metadata dip-channel: api and dip-api-client: <client>. The feed's intake DAG
       validates it like any other landed file (contract, quarantine, bronze).
  401  no key, or a key that matches no client
  403  the client may not send this feed, or the feed's contract does not list `api`
  404  no contract for the feed
  409  landing already holds an unprocessed file of that name
  413  the body is over the size limit (DIP_API_MAX_BYTES, default 20 MB)
  422  a missing or unsafe filename (only letters, digits, `.`, `_` and `-`; no paths)

  GET /healthz   200 {"status": "ok"}

Clients and their keys: DIP_API_CLIENTS is JSON, `{"<client>": {"sha256": "<hex sha256
of the key>", "feeds": ["healthcare.*"]}}`. Only hashes are stored; the API compares in
constant time. Terraform generates one key per pack and writes the hashes to a Secret.
A client may send the feeds matching its patterns (shell-style, `fnmatch`).
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from dip import BUCKET_LANDING, CHANNEL_KEY
from dip.contracts import Contract
from dip.storage import ObjectStore

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class Client:
    name: str
    key_sha256: str
    feeds: tuple[str, ...]

    def may_send(self, feed: str) -> bool:
        return any(fnmatch.fnmatchcase(feed, p) for p in self.feeds)


def load_clients(raw: str) -> list[Client]:
    return [
        Client(name, spec["sha256"].lower(), tuple(spec["feeds"]))
        for name, spec in sorted(json.loads(raw).items())
    ]


def _client_for(clients: list[Client], authorization: str | None) -> Client | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    digest = hashlib.sha256(authorization[len("Bearer ") :].strip().encode()).hexdigest()
    found = None
    for c in clients:  # compare against every client, so timing says nothing
        if hmac.compare_digest(digest, c.key_sha256):
            found = c
    return found


def create_app(
    store: ObjectStore,
    contracts: dict[str, Contract],
    clients: list[Client],
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FastAPI:
    app = FastAPI(title="dip intake API", version="1", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/feeds/{feed}/files", status_code=202)
    async def upload(
        feed: str,
        request: Request,
        filename: str = "",
        authorization: str | None = Header(default=None),
    ) -> Any:
        client = _client_for(clients, authorization)
        if client is None:
            raise HTTPException(401, "missing or unknown API key", {"WWW-Authenticate": "Bearer"})
        # Authorization first: a client learns nothing about feeds outside its own patterns.
        if not client.may_send(feed):
            raise HTTPException(403, f"client {client.name!r} may not send {feed!r}")
        contract = contracts.get(feed)
        if contract is None:
            raise HTTPException(404, f"no contract for feed {feed!r}")
        if "api" not in contract.spec["source"]["channels"]:
            raise HTTPException(403, f"feed {feed!r} does not accept the api channel")
        if not SAFE_NAME.match(filename) or ".." in filename:
            raise HTTPException(422, "filename: letters, digits, '.', '_' and '-' only")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise HTTPException(413, f"over the {max_bytes}-byte limit")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise HTTPException(413, f"over the {max_bytes}-byte limit")
        key = f"{feed}/{filename}"
        data = bytes(body)
        del body
        try:
            # Conditional: two uploads of one name at once cannot overwrite each other.
            store.put(BUCKET_LANDING, key, data, if_absent=True,
                      metadata={CHANNEL_KEY: "api", "dip-api-client": client.name})  # fmt: skip
        except FileExistsError:
            raise HTTPException(409, f"{key} is waiting to be processed; send it later") from None
        return JSONResponse(
            {
                "feed": feed,
                "key": key,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            },
            status_code=202,
        )

    return app


def app_from_env() -> FastAPI:  # pragma: no cover - wiring for the container
    import boto3

    from dip.contracts import load_contracts
    from dip.storage import S3Store

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["DIP_MINIO_URL"],
        aws_access_key_id=os.environ["DIP_MINIO_USER"],
        aws_secret_access_key=os.environ["DIP_MINIO_PASSWORD"],
        region_name="us-east-1",
    )
    return create_app(
        S3Store(s3),
        load_contracts(),
        load_clients(os.environ["DIP_API_CLIENTS"]),
        int(os.environ.get("DIP_API_MAX_BYTES", DEFAULT_MAX_BYTES)),
    )
