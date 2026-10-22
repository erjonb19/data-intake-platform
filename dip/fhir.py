"""Validate a FHIR R4 Bundle against a `fhir_bundle` data contract.

File-level violations reject the whole file. Record-level violations reject one resource
and the rest of the bundle continues (unless the contract says `reject_file`). Reason codes
are stable strings, for example `FHIR.MISSING_FIELD:Encounter.period.start`.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from dip.contracts import Contract

_MISSING = object()


@dataclass(frozen=True)
class Violation:
    reason_code: str
    detail: str
    resource_type: str | None = None
    resource_id: str | None = None
    entry_index: int | None = None


@dataclass(frozen=True)
class Resource:
    entry_index: int
    resource_type: str
    resource_id: str
    full_url: str | None
    body: dict[str, Any]


@dataclass
class BundleResult:
    file_violations: list[Violation] = field(default_factory=list)
    accepted: list[Resource] = field(default_factory=list)
    rejected: list[tuple[Resource, Violation]] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)

    @property
    def file_rejected(self) -> bool:
        return bool(self.file_violations)

    @property
    def patient_id(self) -> str | None:
        return next((r.resource_id for r in self.accepted if r.resource_type == "Patient"), None)


def get_path(obj: Any, path: str) -> Any:
    """`code.coding` -> obj["code"]["coding"]; returns _MISSING when absent or empty."""
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return _MISSING
        obj = obj[part]
    if obj is None or obj == "" or obj == [] or obj == {}:
        return _MISSING
    return obj


def parse_fhir_datetime(value: Any) -> datetime | None:
    """FHIR date or dateTime (YYYY, YYYY-MM, YYYY-MM-DD, or full timestamp) -> aware UTC."""
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 4:
            return datetime(int(value), 1, 1, tzinfo=UTC)
        if len(value) == 7:
            return datetime(int(value[:4]), int(value[5:7]), 1, tzinfo=UTC)
        if len(value) == 10:
            d = date.fromisoformat(value)
            return datetime(d.year, d.month, d.day, tzinfo=UTC)
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _reference_target(resource: dict[str, Any], ref_field: str) -> str | None:
    ref = resource.get(ref_field)
    return ref.get("reference") if isinstance(ref, dict) else None


def _check_resource(
    res: Resource,
    rules: dict[str, Any],
    received_at: datetime,
    window: tuple[datetime, datetime] | None = None,
) -> Violation | None:
    """`window`: for sources whose dates are deliberately shifted (MIMIC moves every
    patient into 2100-2200), dates must fall inside it instead of not after arrival."""
    rt, body = res.resource_type, res.body

    def v(code: str, detail: str) -> Violation:
        return Violation(code, detail, rt, res.resource_id, res.entry_index)

    for path in rules.get("required", []):
        if get_path(body, path) is _MISSING:
            return v(f"FHIR.MISSING_FIELD:{rt}.{path}", f"{rt}/{res.resource_id} has no {path}")
    if (one_of := rules.get("required_one_of")) and all(
        get_path(body, p) is _MISSING for p in one_of
    ):
        return v(f"FHIR.MISSING_FIELD:{rt}.{'|'.join(one_of)}", f"needs one of {one_of}")
    for path, allowed in rules.get("allowed", {}).items():
        value = get_path(body, path)
        if value is not _MISSING and value not in allowed:
            return v(f"FHIR.INVALID_VALUE:{rt}.{path}", f"{path}={value!r} not in {allowed}")
    if systems := rules.get("code_systems"):
        codings = get_path(body, "code.coding")
        if codings is _MISSING:
            codings = get_path(body, "medicationCodeableConcept.coding")
        if codings is not _MISSING and not any(
            isinstance(c, dict) and c.get("system") in systems and c.get("code") for c in codings
        ):
            found = sorted({str(c.get("system")) for c in codings if isinstance(c, dict)})
            return v(f"FHIR.UNKNOWN_CODE_SYSTEM:{rt}.code", f"systems {found}, expected {systems}")
    for path in rules.get("dates", []):
        raw = get_path(body, path)
        if raw is _MISSING:
            continue
        parsed = parse_fhir_datetime(raw)
        if parsed is None:
            return v(f"FHIR.INVALID_DATE:{rt}.{path}", f"{path}={raw!r} is not a FHIR date")
        if window is not None:
            if not window[0] <= parsed <= window[1]:
                return v(f"FHIR.DATE_OUT_OF_WINDOW:{rt}.{path}", f"{path}={raw} outside {window}")
        elif parsed > received_at:
            return v(f"FHIR.FUTURE_DATE:{rt}.{path}", f"{path}={raw} is after the file arrived")
    return None


def validate_bundle(data: bytes, contract: Contract, received_at: datetime) -> BundleResult:
    result = BundleResult()
    spec = contract.spec["fhir"]
    fmt = contract.spec["format"]

    def reject_file(code: str, detail: str) -> BundleResult:
        result.file_violations.append(Violation(code, detail))
        result.accepted.clear()
        result.rejected.clear()
        return result

    try:
        bundle = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return reject_file("FILE.INVALID_JSON", str(exc)[:200])
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        found = bundle.get("resourceType") if isinstance(bundle, dict) else type(bundle).__name__
        return reject_file("FHIR.NOT_A_BUNDLE", f"resourceType is {found!r}")
    if bundle.get("type") not in fmt.get("bundle_types", [bundle.get("type")]):
        return reject_file(f"FHIR.BUNDLE_TYPE:{bundle.get('type')}", "bundle type not allowed")
    entries = bundle.get("entry")
    if not isinstance(entries, list) or not entries:
        return reject_file("FHIR.EMPTY_BUNDLE", "bundle has no entries")

    load_types = set(spec["load"])
    candidates: list[Resource] = []
    for i, entry in enumerate(entries):
        body = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(body, dict) or "resourceType" not in body:
            return reject_file("FHIR.MALFORMED_ENTRY", f"entry {i} has no resource")
        rt = body["resourceType"]
        if rt not in load_types:
            result.skipped[rt] += 1
            continue
        candidates.append(Resource(i, rt, str(body.get("id", "")), entry.get("fullUrl"), body))

    patients = [r for r in candidates if r.resource_type == spec["patient_resource"]]
    if len(patients) != 1:
        return reject_file(f"FHIR.PATIENT_COUNT:{len(patients)}", "expected exactly one patient")

    rules = spec["resources"]
    window = None
    if dw := spec.get("date_window"):
        window = (parse_fhir_datetime(str(dw["min"])), parse_fhir_datetime(str(dw["max"])))
    seen: set[tuple[str, str]] = set()
    passed: list[Resource] = []
    for res in candidates:
        violation = _check_resource(res, rules.get(res.resource_type, {}), received_at, window)
        key = (res.resource_type, res.resource_id)
        if violation is None and key in seen:
            violation = Violation(
                f"FHIR.DUPLICATE_ID:{res.resource_type}",
                f"{res.resource_type}/{res.resource_id} appears twice",
                res.resource_type,
                res.resource_id,
                res.entry_index,
            )
        seen.add(key)
        if violation is None:
            passed.append(res)
        elif res.resource_type == spec["patient_resource"]:
            return reject_file(f"FHIR.PATIENT_INVALID:{violation.reason_code}", violation.detail)
        else:
            result.rejected.append((res, violation))

    # References must resolve to an accepted resource of the right type. Rejecting an
    # Encounter therefore also rejects the Conditions recorded during it.
    ref_rules: dict[str, str] = spec.get("references", {})
    while True:
        targets: dict[str, str] = {}
        for r in passed:
            for alias in (r.full_url, f"{r.resource_type}/{r.resource_id}"):
                if alias:
                    targets[alias] = r.resource_type
        still: list[Resource] = []
        for res in passed:
            violation = None
            for ref_field, target_type in ref_rules.items():
                ref = _reference_target(res.body, ref_field)
                if ref is not None and targets.get(ref) != target_type:
                    violation = Violation(
                        f"FHIR.UNRESOLVED_REFERENCE:{res.resource_type}.{ref_field}",
                        f"{ref_field} -> {ref} is not an accepted {target_type} in this bundle",
                        res.resource_type,
                        res.resource_id,
                        res.entry_index,
                    )
                    break
            if violation is None:
                still.append(res)
            else:
                result.rejected.append((res, violation))
        if len(still) == len(passed):
            break
        passed = still

    checked = len(passed) + len(result.rejected)
    bad = len(result.rejected)
    if bad and contract.rejects_file_on_row_violation:
        return reject_file(
            "CONTRACT.ROW_VIOLATION", f"{bad} invalid resources; contract rejects file"
        )
    if checked and bad / checked > contract.max_bad_row_ratio:
        return reject_file(
            "CONTRACT.BAD_ROW_RATIO",
            f"{bad}/{checked} resources invalid, above max {contract.max_bad_row_ratio}",
        )
    result.accepted = passed
    return result
