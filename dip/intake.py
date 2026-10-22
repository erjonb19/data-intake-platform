"""The intake flow for one file: land a raw copy, validate, then load or quarantine.

Files arrive at `landing/<feed>/<filename>`. The raw copy is written to
`raw/<feed>/<received date>/<sha256><ext>` before anything else and is never changed, so
any run can be replayed. Content that was already processed (same feed and sha256) is
not loaded again, which makes reruns safe. If it comes back under a new name (a full-file
feed resending identical content as a newer dated file), that is recorded as a new
delivery, so the newer business date still counts (ops.intake_delivery).

A contract can declare a business date in the file name (`source.filename_date`); a name
whose date is not a real date, is in the future, or is too far before the received date is
quarantined with FILE.NAME_DATE.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import PurePosixPath

from dip import BUCKET_LANDING, BUCKET_QUARANTINE, BUCKET_RAW, CHANNEL_KEY
from dip.contracts import Contract
from dip.csvfeed import CsvResult, Row, validate_csv
from dip.excelfeed import validate_excel
from dip.fhir import BundleResult, Resource, Violation, validate_bundle
from dip.storage import ObjectStore
from dip.warehouse import IntakeRecord, QuarantineRecord, Warehouse


@dataclass(frozen=True)
class Outcome:
    source_key: str
    file_sha256: str
    status: str  # loaded | loaded_with_rejections | quarantined | duplicate | redelivered
    accepted: int = 0
    rejected: int = 0
    reason_codes: tuple[str, ...] = ()
    raw_key: str = ""
    quarantine_keys: tuple[str, ...] = ()


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

    if not store.exists(BUCKET_RAW, raw_key):
        store.put(BUCKET_RAW, raw_key, data)

    channel = store.metadata(BUCKET_LANDING, landing_key).get(CHANNEL_KEY, "minio")
    file_date, name_problems = _check_name(filename, contract, now)
    name_problems = _check_channel(channel, contract) + name_problems
    if name_problems:
        # Checked before the duplicate check and not recorded in ops.intake_file: the
        # content was never validated, so the same bytes under a correct name (or through
        # an allowed channel) still load.
        qkey = f"{contract.feed}/{day}/{sha}.{_slug(filename)}{ext}"
        result = BundleResult()
        result.file_violations.extend(name_problems)
        store.put(BUCKET_QUARANTINE, qkey, data)
        store.put(BUCKET_QUARANTINE, f"{qkey}.reason.json", _reason_doc(result, landing_key))
        warehouse.record_quarantine(
            [
                QuarantineRecord(contract.feed, sha, "file", v.reason_code, v.detail, None, qkey)
                for v in name_problems
            ]
        )
        store.delete(BUCKET_LANDING, landing_key)
        return Outcome(
            landing_key,
            sha,
            "quarantined",
            reason_codes=tuple(sorted({v.reason_code for v in name_problems})),
            raw_key=raw_key,
            quarantine_keys=(qkey,),
        )

    if warehouse.already_processed(contract.feed, sha):
        delivery = _intake_record(
            contract, sha, landing_key, raw_key, "", now, file_date, channel=channel
        )
        new = warehouse.record_delivery(delivery)
        store.delete(BUCKET_LANDING, landing_key)
        return Outcome(landing_key, sha, "redelivered" if new else "duplicate", raw_key=raw_key)

    if contract.format_type == "fhir_bundle":
        result = validate_bundle(data, contract, received_at=now)
    elif contract.format_type == "csv":
        result = validate_csv(data, contract, received_at=now)
    elif contract.format_type == "excel":
        result = validate_excel(data, contract, received_at=now)
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
        if isinstance(result, CsvResult):
            # Rejected for its rows (FILE.BAD_ROW_RATIO, FILE.ROW_VIOLATIONS): each bad
            # row keeps its own reason, pointing at the quarantined file.
            quarantine += [
                QuarantineRecord(
                    contract.feed, sha, "record", v.reason_code, v.detail, _record_ref(r), qkey
                )
                for r, v in result.rejected
            ]
        status = "quarantined"
    else:
        if result.rejected:
            qkey = f"{qbase}.rejected.ndjson"
            lines = (json.dumps(_rejected_doc(r, v), sort_keys=True) for r, v in result.rejected)
            store.put(BUCKET_QUARANTINE, qkey, ("\n".join(lines) + "\n").encode("utf-8"))
            quarantine = [
                QuarantineRecord(
                    contract.feed, sha, "record", v.reason_code, v.detail, _record_ref(r), qkey
                )
                for r, v in result.rejected
            ]
        status = "loaded_with_rejections" if result.rejected else "loaded"

    csv_mode = isinstance(result, CsvResult)
    warehouse.record_outcome(
        _intake_record(
            contract,
            sha,
            landing_key,
            raw_key,
            status,
            now,
            file_date,
            accepted=len(result.accepted),
            rejected=len(result.rejected),
            skipped=sum(result.skipped.values()),
            channel=channel,
        ),
        quarantine,
        [] if csv_mode else result.accepted,
        result.patient_id,
        loaded_at=now,
        csv_rows=result.accepted if csv_mode else [],
    )
    store.delete(BUCKET_LANDING, landing_key)
    return Outcome(
        landing_key,
        sha,
        status,
        accepted=len(result.accepted),
        rejected=len(result.rejected),
        reason_codes=tuple(sorted({q.reason_code for q in quarantine})),
        raw_key=raw_key,
        quarantine_keys=tuple(sorted({q.quarantine_key for q in quarantine})),
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


def _rejected_doc(r: Row | Resource, v: Violation) -> dict:
    if isinstance(r, Row):
        return {"reason_code": v.reason_code, "row_number": r.row_number, "record": r.record}
    return {"reason_code": v.reason_code, "resource": r.body}


def _record_ref(r: Row | Resource) -> str:
    if isinstance(r, Row):
        return f"row={r.row_number}"
    return f"{r.resource_type}/{r.resource_id}#entry={r.entry_index}"


def _check_channel(channel: str, contract: Contract) -> list[Violation]:
    """A file must arrive through a channel its contract lists (`source.channels`)."""
    allowed = contract.spec["source"]["channels"]
    if channel in allowed:
        return []
    return [
        Violation(f"FILE.CHANNEL:{channel}", f"arrived by {channel}; contract allows {allowed}")
    ]


def _check_name(
    filename: str, contract: Contract, now: datetime
) -> tuple[date | None, list[Violation]]:
    """The file name's business date, and its violations (pattern, then date)."""
    if not contract.matches_filename(filename):
        pattern = contract.spec["source"]["filename_pattern"]
        return None, [Violation("FILE.NAME_PATTERN", f"{filename!r} does not match {pattern!r}")]
    file_date, problem = contract.filename_date(filename, now.astimezone(UTC).date())
    return file_date, [Violation("FILE.NAME_DATE", problem)] if problem else []


def _slug(filename: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in PurePosixPath(filename).stem)


def _intake_record(
    contract: Contract,
    sha: str,
    landing_key: str,
    raw_key: str,
    status: str,
    now: datetime,
    file_date: date | None,
    accepted: int = 0,
    rejected: int = 0,
    skipped: int = 0,
    channel: str = "minio",
) -> IntakeRecord:
    return IntakeRecord(
        file_sha256=sha,
        feed=contract.feed,
        contract_version=contract.version,
        source_key=landing_key,
        raw_key=raw_key,
        status=status,
        accepted_count=accepted,
        rejected_count=rejected,
        skipped_count=skipped,
        received_at=now,
        file_date=file_date,
        channel=channel,
    )


def _reason_doc(result: BundleResult | CsvResult, landing_key: str) -> bytes:
    doc = {
        "source_key": landing_key,
        "violations": [
            {"reason_code": v.reason_code, "detail": v.detail} for v in result.file_violations
        ],
    }
    if isinstance(result, CsvResult) and result.rejected:
        doc["rows"] = [
            {"row_number": r.row_number, "reason_code": v.reason_code} for r, v in result.rejected
        ]
    return json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")
