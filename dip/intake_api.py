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
  429  24.32, with Retry-After: the client sent more than DIP_API_RATE_PER_MIN uploads (60)
       in the last minute; or DIP_API_MAX_INFLIGHT uploads (4) are being received already,
       which bounds the memory they take; or this address had DIP_API_AUTH_FAILS_PER_MIN
       (20) requests refused with 401 in the last minute (dip/ratelimit.py)

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
from fastapi.responses import JSONResponse, PlainTextResponse

from dip import BUCKET_LANDING, CHANNEL_KEY
from dip.contracts import Contract
from dip.ratelimit import FailedAuthLimit, Window
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
    rate_per_minute: int = 60,
    max_inflight: int = 4,
    auth_fails_per_minute: int = 20,
) -> FastAPI:
    # 24.34: no /docs, /redoc or /openapi.json: they answer without a key and list every
    # route and parameter to anyone who can reach the Service.
    app = FastAPI(title="dip intake API", version="1", docs_url=None, redoc_url=None,
                  openapi_url=None)  # fmt: skip
    # 24.18: refused requests, by why, for Prometheus (the IntakeApiAuthFailures alert).
    # In this process only: a restart starts them at zero, which increase() handles.
    refused = {"unauthorized": 0, "forbidden": 0, "rate_limited": 0, "busy": 0}
    app.add_middleware(FailedAuthLimit, limit=auth_fails_per_minute)
    per_client = Window(rate_per_minute, 60.0)
    inflight = {"n": 0}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        """Counts only, no feed or client names: safe without a key, like /healthz."""
        lines = [
            "# HELP dip_intake_api_refused_total Upload requests refused, by reason.",
            "# TYPE dip_intake_api_refused_total counter",
        ]
        lines += [f'dip_intake_api_refused_total{{reason="{k}"}} {v}' for k, v in refused.items()]
        return "\n".join(lines) + "\n"

    @app.post("/v1/feeds/{feed}/files", status_code=202)
    async def upload(
        feed: str,
        request: Request,
        filename: str = "",
        authorization: str | None = Header(default=None),
    ) -> Any:
        client = _client_for(clients, authorization)
        if client is None:
            refused["unauthorized"] += 1
            raise HTTPException(401, "missing or unknown API key", {"WWW-Authenticate": "Bearer"})
        # Authorization first: a client learns nothing about feeds outside its own patterns.
        if not client.may_send(feed):
            refused["forbidden"] += 1
            raise HTTPException(403, f"client {client.name!r} may not send {feed!r}")
        contract = contracts.get(feed)
        if contract is None:
            raise HTTPException(404, f"no contract for feed {feed!r}")
        if "api" not in contract.spec["source"]["channels"]:
            raise HTTPException(403, f"feed {feed!r} does not accept the api channel")
        if not SAFE_NAME.match(filename) or ".." in filename:
            raise HTTPException(422, "filename: letters, digits, '.', '_' and '-' only")
        # Counted only for an upload that would otherwise be taken (a typo does not use it up).
        if not per_client.hit(client.name):
            refused["rate_limited"] += 1
            wait = str(per_client.retry_after(client.name) or 60)
            raise HTTPException(
                429, f"over {rate_per_minute} uploads a minute", {"Retry-After": wait}
            )
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise HTTPException(413, f"over the {max_bytes}-byte limit")
        # 24.32: each upload is held in memory until it is stored, so only so many at once.
        if inflight["n"] >= max_inflight:
            refused["busy"] += 1
            raise HTTPException(429, "too many uploads in progress; send it again shortly",
                                {"Retry-After": "5"})  # fmt: skip
        inflight["n"] += 1
        try:  # the slot is held until the file is stored, while its bytes are in memory
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise HTTPException(413, f"over the {max_bytes}-byte limit")
            data = bytes(body)
            del body
            key = f"{feed}/{filename}"
            try:
                # Conditional: two uploads of one name at once cannot overwrite each other.
                store.put(BUCKET_LANDING, key, data, if_absent=True,
                          metadata={CHANNEL_KEY: "api", "dip-api-client": client.name})  # fmt: skip
            except FileExistsError:
                raise HTTPException(
                    409, f"{key} is waiting to be processed; send it later"
                ) from None
            return JSONResponse(
                {
                    "feed": feed,
                    "key": key,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                },
                status_code=202,
            )
        finally:
            inflight["n"] -= 1

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
        rate_per_minute=int(os.environ.get("DIP_API_RATE_PER_MIN", "60")),
        max_inflight=int(os.environ.get("DIP_API_MAX_INFLIGHT", "4")),
        auth_fails_per_minute=int(os.environ.get("DIP_API_AUTH_FAILS_PER_MIN", "20")),
    )
