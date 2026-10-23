"""Validate an X12 5010 interchange (837P claims or 835 remittance) against an `x12` data
contract (Stage 4).

The interchange is read in two steps:

1. **Envelope.** The ISA header is fixed width (106 characters) and names the separators:
   the element separator is its 4th character, the component separator its 105th, the
   segment terminator its 106th. Then every envelope count and control number must agree:
   ISA13 = IEA02, IEA01 = the number of GS groups, GS06 = GE02, GE01 = the number of ST
   transactions in the group, ST02 = SE02, and SE01 = the number of segments from ST to SE.
   Every transaction must be the contract's (`format.transaction`: 837 or 835) with its
   implementation guide (`format.version`, e.g. 005010X222A1). Any failure quarantines the
   whole file: a broken envelope means segments were lost or added in transit, and no row
   in it can be trusted.
2. **Rows.** Each 837P service line (LX/SV1) or 835 service payment (SVC) becomes one row of
   text, in the column order below, and goes through the CSV engine's header, row and
   file checks (`csvfeed.check_rows`): a bad value gets the same `VALUE.*` code as in any
   other format, and with `quarantine_row` a bad line is quarantined on its own. Dates
   (CCYYMMDD) become ISO dates; one that is not a real date is kept as received and fails
   the date check. Amounts are kept as received.

A claim line is matched across the two transactions by its line item control number
(REF*6R, `claim_line_id`), as payers and providers do.

Reason codes added by this format (all file level):
  X12.INVALID             not an X12 interchange: no ISA header of the right shape
  X12.ENVELOPE:<element>  an envelope count or control number disagrees (IEA01, IEA02,
                          GE01, GE02, SE01, SE02), or a segment is missing or out of place
                          (ISA, GS, ST, SE, GE, IEA)
  X12.TRANSACTION:<id>    a transaction set other than the contract's (an 835 sent to the
                          837 feed), or another implementation guide version
  FILE.ENCODING           not ASCII/UTF-8 text
  FILE.TOO_LARGE          over MAX_BYTES, or more than MAX_SEGMENTS segments

All X12 here is synthetic (packs/healthcare/generators/x12.py).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from dip.csvfeed import CsvResult, check_rows
from dip.fhir import Violation

MAX_BYTES = 50 * 1024 * 1024
MAX_SEGMENTS = 2_000_000
ISA_LENGTH = 106

COLUMNS_837 = (
    "claim_id",
    "claim_line_id",
    "line_number",
    "member_id",
    "payer_id",
    "billing_npi",
    "place_of_service",
    "claim_frequency",
    "claim_total",
    "procedure_qualifier",
    "procedure_code",
    "charge_amount",
    "units",
    "service_date",
)
COLUMNS_835 = (
    "payment_trace",
    "payment_date",
    "payer_name",
    "claim_id",
    "claim_status",
    "payer_claim_control",
    "claim_line_id",
    "procedure_code",
    "charge_amount",
    "paid_amount",
    "contractual_adjustment",
    "deductible_amount",
    "coinsurance_amount",
    "other_adjustment",
    "service_date",
)
COLUMNS = {"837": COLUMNS_837, "835": COLUMNS_835}


class _Refused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.violation = Violation(code, detail)


@dataclass
class Interchange:
    element: str
    component: str
    segments: list[list[str]]  # each segment's elements; [0] is its id
    transactions: list[list[list[str]]] = field(default_factory=list)  # ST..SE, inclusive


def parse(data: bytes, transaction: str | None = None, version: str | None = None) -> Interchange:
    """The interchange's segments, after every envelope check; raises _Refused."""
    if len(data) > MAX_BYTES:
        raise _Refused("FILE.TOO_LARGE", f"{len(data)} bytes, limit {MAX_BYTES}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Refused("FILE.ENCODING", f"not ASCII/UTF-8 text: {exc.reason}") from exc
    text = text.lstrip("﻿\r\n\t ")
    if len(text) < ISA_LENGTH or not text.startswith("ISA"):
        raise _Refused("X12.INVALID", "no ISA interchange header at the start of the file")
    element, component, terminator = text[3], text[104], text[105]
    if len({element, component, terminator}) < 3 or any(c.isalnum() for c in (element, terminator)):
        raise _Refused("X12.INVALID", "the ISA header does not define three distinct separators")
    isa = text[: ISA_LENGTH - 1].split(element)
    if len(isa) != 17:
        raise _Refused("X12.INVALID", f"the ISA header has {len(isa) - 1} elements, not 16")
    raw = [s.strip("\r\n") for s in text.split(terminator)]
    raw = [s for s in raw if s]
    if len(raw) > MAX_SEGMENTS:
        raise _Refused("FILE.TOO_LARGE", f"more than {MAX_SEGMENTS} segments")
    segments = [s.split(element) for s in raw]
    ic = Interchange(element, component, segments)
    _check_envelope(ic, transaction, version)
    return ic


def _envelope_error(element: str, detail: str) -> _Refused:
    return _Refused(f"X12.ENVELOPE:{element}", detail)


def _el(seg: list[str], n: int) -> str:
    return seg[n].strip() if len(seg) > n else ""


def _check_envelope(ic: Interchange, transaction: str | None, version: str | None) -> None:
    segs = ic.segments
    if segs[-1][0] != "IEA":
        raise _envelope_error("IEA", "the interchange does not end with an IEA trailer")
    isa, iea = segs[0], segs[-1]
    if _el(isa, 13) != _el(iea, 2):
        raise _envelope_error(
            "IEA02", f"IEA02 {_el(iea, 2)!r} is not the ISA13 control number {_el(isa, 13)!r}"
        )
    groups, i = 0, 1
    while i < len(segs) - 1:
        gs = segs[i]
        if gs[0] != "GS":
            raise _envelope_error("GS", f"segment {i + 1} is {gs[0]}, expected GS")
        groups += 1
        i += 1
        sets = 0
        while i < len(segs) - 1 and segs[i][0] == "ST":
            st, start = segs[i], i
            while i < len(segs) - 1 and segs[i][0] not in ("SE", "GE", "IEA"):
                i += 1
                if segs[i][0] == "ST":
                    raise _envelope_error("SE", f"transaction {_el(st, 2)!r} has no SE trailer")
            se = segs[i]
            if se[0] != "SE":
                raise _envelope_error("SE", f"transaction {_el(st, 2)!r} has no SE trailer")
            count = i - start + 1
            if _el(se, 1) != str(count):
                raise _envelope_error(
                    "SE01",
                    f"SE01 says {_el(se, 1)!r} segments; transaction {_el(st, 2)!r} has {count}",
                )
            if _el(se, 2) != _el(st, 2):
                raise _envelope_error(
                    "SE02", f"SE02 {_el(se, 2)!r} is not the ST02 control number {_el(st, 2)!r}"
                )
            if transaction and _el(st, 1) != transaction:
                raise _Refused(
                    f"X12.TRANSACTION:{_el(st, 1)}",
                    f"transaction set {_el(st, 1)}; this feed takes {transaction}",
                )
            if version and _el(st, 3) not in (version, ""):
                raise _Refused(
                    f"X12.TRANSACTION:{_el(st, 1)}",
                    f"implementation guide {_el(st, 3)!r}; this feed takes {version!r}",
                )
            ic.transactions.append(segs[start : i + 1])
            sets += 1
            i += 1
        if i >= len(segs) - 1 or segs[i][0] != "GE":
            found = segs[i][0] if i < len(segs) else "end of file"
            raise _envelope_error("GE", f"group {_el(gs, 6)!r} has no GE trailer (found {found})")
        ge = segs[i]
        if _el(ge, 1) != str(sets):
            raise _envelope_error(
                "GE01", f"GE01 says {_el(ge, 1)!r} transactions; group {_el(gs, 6)!r} has {sets}"
            )
        if _el(ge, 2) != _el(gs, 6):
            raise _envelope_error(
                "GE02", f"GE02 {_el(ge, 2)!r} is not the GS06 control number {_el(gs, 6)!r}"
            )
        i += 1
    if _el(iea, 1) != str(groups):
        raise _envelope_error("IEA01", f"IEA01 says {_el(iea, 1)!r} groups; there are {groups}")
    if not ic.transactions:
        raise _envelope_error("ST", "the interchange has no transaction set")


def _iso(d8: str) -> str:
    """CCYYMMDD as an ISO date; anything else as received (the date check then fails it)."""
    if re.fullmatch(r"[0-9]{8}", d8):
        try:
            return date(int(d8[:4]), int(d8[4:6]), int(d8[6:])).isoformat()
        except ValueError:
            pass
    return d8


def _rows_837(ic: Interchange) -> list[list[str]]:
    out: list[list[str]] = []
    for tx in ic.transactions:
        ctx: dict[str, str] = {}
        line: dict[str, str] | None = None

        def flush() -> None:
            nonlocal line
            if line is not None:
                out.append([line.get(c, "") for c in COLUMNS_837])
            line = None

        for seg in tx:
            sid = seg[0]
            if sid in ("HL", "CLM", "LX", "SE"):
                flush()
            if sid == "NM1":
                qualifier, ident = _el(seg, 1), _el(seg, 9)
                if qualifier == "85":
                    ctx["billing_npi"] = ident
                elif qualifier == "IL":
                    ctx["member_id"] = ident
                elif qualifier == "PR":
                    ctx["payer_id"] = ident
            elif sid == "CLM":
                facility = _el(seg, 5).split(ic.component)
                ctx.update(
                    claim_id=_el(seg, 1),
                    claim_total=_el(seg, 2),
                    place_of_service=facility[0] if facility else "",
                    claim_frequency=facility[2] if len(facility) > 2 else "",
                )
            elif sid == "LX":
                line = {**ctx, "line_number": _el(seg, 1)}
            elif sid == "SV1" and line is not None:
                proc = _el(seg, 1).split(ic.component)
                line.update(
                    procedure_qualifier=proc[0],
                    procedure_code=proc[1] if len(proc) > 1 else "",
                    charge_amount=_el(seg, 2),
                    units=_el(seg, 4),
                )
            elif sid == "DTP" and line is not None and _el(seg, 1) == "472":
                line["service_date"] = _iso(_el(seg, 3))
            elif sid == "REF" and line is not None and _el(seg, 1) == "6R":
                line["claim_line_id"] = _el(seg, 2)
        flush()
    return out


def _rows_835(ic: Interchange) -> list[list[str]]:
    out: list[list[str]] = []
    for tx in ic.transactions:
        ctx: dict[str, str] = {}
        line: dict[str, Any] | None = None

        def flush() -> None:
            nonlocal line
            if line is not None:
                adj = line.pop("_adjustments")
                line.update(
                    contractual_adjustment=_sum(adj.get(("CO", "45"), [])),
                    deductible_amount=_sum(adj.get(("PR", "1"), [])),
                    coinsurance_amount=_sum(adj.get(("PR", "2"), [])),
                    other_adjustment=_sum(
                        [
                            a
                            for k, v in adj.items()
                            if k not in (("CO", "45"), ("PR", "1"), ("PR", "2"))
                            for a in v
                        ]
                    ),
                )
                out.append([str(line.get(c, "")) for c in COLUMNS_835])
            line = None

        for seg in tx:
            sid = seg[0]
            if sid in ("CLP", "SVC", "LX", "SE", "PLB"):
                flush()
            if sid == "BPR":
                ctx["payment_date"] = _iso(_el(seg, 16))
            elif sid == "TRN":
                ctx["payment_trace"] = _el(seg, 2)
            elif sid == "N1" and _el(seg, 1) == "PR":
                ctx["payer_name"] = _el(seg, 2)
            elif sid == "CLP":
                ctx.update(
                    claim_id=_el(seg, 1), claim_status=_el(seg, 2), payer_claim_control=_el(seg, 7)
                )
            elif sid == "SVC":
                proc = _el(seg, 1).split(ic.component)
                line = {
                    **ctx,
                    "procedure_code": proc[1] if len(proc) > 1 else "",
                    "charge_amount": _el(seg, 2),
                    "paid_amount": _el(seg, 3),
                    "_adjustments": defaultdict(list),
                }
            elif sid == "DTM" and line is not None and _el(seg, 1) == "472":
                line["service_date"] = _iso(_el(seg, 2))
            elif sid == "CAS" and line is not None:
                group = _el(seg, 1)
                for n in range(2, len(seg), 3):  # (reason, amount, quantity) triplets
                    reason, amount = _el(seg, n), _el(seg, n + 1)
                    if reason:
                        line["_adjustments"][(group, reason)].append(amount)
            elif sid == "REF" and line is not None and _el(seg, 1) == "6R":
                line["claim_line_id"] = _el(seg, 2)
        flush()
    return out


def _sum(amounts: list[str]) -> str:
    """The adjustments' total as text; one that is not a number stays as received, so the
    contract's decimal check rejects the line."""
    try:
        total = sum((Decimal(a) for a in amounts), Decimal("0"))
    except InvalidOperation:
        return next(a for a in amounts if not _is_decimal(a))
    return f"{total:.2f}"


def _is_decimal(text: str) -> bool:
    try:
        Decimal(text)
    except InvalidOperation:
        return False
    return True


def rows(data: bytes, transaction: str, version: str | None = None) -> list[list[str]]:
    """The file's rows of text (the header first), or raises _Refused."""
    ic = parse(data, transaction, version)
    body = _rows_837(ic) if transaction == "837" else _rows_835(ic)
    return [list(COLUMNS[transaction]), *body]


def validate_x12(data: bytes, contract: Any, received_at: datetime | None = None) -> CsvResult:
    spec = contract.spec
    fmt = spec["format"]
    today = (received_at or datetime.now(UTC)).astimezone(UTC).date()
    result = CsvResult()
    try:
        table = rows(data, fmt["transaction"], fmt.get("version"))
    except _Refused as refused:
        result.file_violations.append(refused.violation)
        return result
    return check_rows(table, spec, today, result)
