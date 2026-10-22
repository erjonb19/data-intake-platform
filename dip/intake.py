"""The intake flow for one file: land a raw copy, validate, then load or quarantine.

Files arrive at `landing/<feed>/<filename>`. The raw copy is written to
`raw/<feed>/<received date>/<sha256><ext>` before anything else and is never changed, so
any run can be replayed. A file that was already processed (same feed and sha256) is
skipped, which makes reruns safe.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from dip import BUCKET_LANDING, BUCKET_QUARANTINE, BUCKET_RAW
from dip.contracts import Contract
from dip.fhir import BundleResult, Violation, validate_bundle
from dip.storage import ObjectStore
from dip.warehouse import IntakeRecord, QuarantineRecord, Warehouse


@dataclass(frozen=True)
class Outcome:
    source_key: str
    file_sha256: str
    status: str  # loaded | loaded_with_rejections | quarantined | duplicate
    accepted: int = 0
    rejected: int = 0
    reason_codes: tuple[str, ...] = ()


def feed_of(landing_key: str) -> str:
    parts = PurePosixPath(landing_key).parts
    if len(parts) < 2:
        raise ValueError(f"landing key must be <feed>/<filename>: {landing_key!r}")
    return parts[0]


def process_file(
    store: ObjectStore,
    warehouse: Warehouse,
    contract: Contract,
    landing_key: str,
    now: datetime | None = None,
) -> Outcome:
    now = now or datetime.now(UTC)
    data = store.get(BUCKET_LANDING, landing_key)
    sha = hashlib.sha256(data).hexdigest()
    filename = PurePosixPath(landing_key).name
    ext = PurePosixPath(filename).suffix
    day = now.strftime("%Y-%m-%d")
    raw_key = f"{contract.feed}/{day}/{sha}{ext}"

    if warehouse.already_processed(contract.feed, sha):
        store.delete(BUCKET_LANDING, landing_key)
        return Outcome(landing_key, sha, "duplicate")

    if not store.exists(BUCKET_RAW, raw_key):
        store.put(BUCKET_RAW, raw_key, data)

    if not contract.matches_filename(filename):
        result = BundleResult()
        result.file_violations.append(_name_violation(filename, contract))
    elif contract.format_type == "fhir_bundle":
        result = validate_bundle(data, contract, received_at=now)
    else:
        raise NotImplementedError(f"format {contract.format_type!r} arrives in a later stage")

    quarantine: list[QuarantineRecord] = []
    qbase = f"{contract.feed}/{day}/{sha}"
    if result.file_rejected:
        qkey = f"{qbase}{ext}"
        store.put(BUCKET_QUARANTINE, qkey, data)
        store.put(BUCKET_QUARANTINE, f"{qbase}.reason.json", _reason_doc(result, landing_key))
        quarantine = [
            QuarantineRecord(contract.feed, sha, "file", v.reason_code, v.detail, None, qkey)
            for v in result.file_violations
        ]
        status = "quarantined"
    else:
        if result.rejected:
            qkey = f"{qbase}.rejected.ndjson"
            lines = (
                json.dumps({"reason_code": v.reason_code, "resource": r.body}, sort_keys=True)
                for r, v in result.rejected
            )
            store.put(BUCKET_QUARANTINE, qkey, ("\n".join(lines) + "\n").encode("utf-8"))
            quarantine = [
                QuarantineRecord(
                    contract.feed,
                    sha,
                    "record",
                    v.reason_code,
                    v.detail,
                    f"{r.resource_type}/{r.resource_id}#entry={r.entry_index}",
                    qkey,
                )
                for r, v in result.rejected
            ]
        status = "loaded_with_rejections" if result.rejected else "loaded"

    warehouse.record_outcome(
        IntakeRecord(
            file_sha256=sha,
            feed=contract.feed,
            contract_version=contract.version,
            source_key=landing_key,
            raw_key=raw_key,
            status=status,
            accepted_count=len(result.accepted),
            rejected_count=len(result.rejected),
            skipped_count=sum(result.skipped.values()),
            received_at=now,
        ),
        quarantine,
        result.accepted,
        result.patient_id,
        loaded_at=now,
    )
    store.delete(BUCKET_LANDING, landing_key)
    return Outcome(
        landing_key,
        sha,
        status,
        accepted=len(result.accepted),
        rejected=len(result.rejected),
        reason_codes=tuple(sorted({q.reason_code for q in quarantine})),
    )


def process_landing(
    store: ObjectStore,
    warehouse: Warehouse,
    contracts: dict[str, Contract],
    now: datetime | None = None,
) -> list[Outcome]:
    """Process every file waiting in the landing bucket, in key order."""
    outcomes = []
    for key in store.list(BUCKET_LANDING):
        feed = feed_of(key)
        if feed not in contracts:
            raise KeyError(f"no contract for feed {feed!r} (file {key})")
        outcomes.append(process_file(store, warehouse, contracts[feed], key, now=now))
    return outcomes


def _name_violation(filename: str, contract: Contract) -> Violation:
    pattern = contract.spec["source"]["filename_pattern"]
    return Violation("FILE.NAME_PATTERN", f"{filename!r} does not match {pattern!r}")


def _reason_doc(result: BundleResult, landing_key: str) -> bytes:
    doc = {
        "source_key": landing_key,
        "violations": [
            {"reason_code": v.reason_code, "detail": v.detail} for v in result.file_violations
        ],
    }
    return json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")
