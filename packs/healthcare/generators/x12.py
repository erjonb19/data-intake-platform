"""Generate synthetic X12 5010 837P (professional claims) and 835 (remittance) files (Stage 4).

Both come from a claims file in the `healthcare.claims` layout (claims.py, from Synthea's
ExplanationOfBenefit resources), so they describe the same claim lines:

  837P   one transaction per payer; under the billing provider (HL 20), one subscriber
         loop (HL 22) per member, one CLM per claim, and one LX/SV1 per line with its
         service date (DTP*472) and line item control number (REF*6R = claim_line_id)
  835    one transaction per payer (BPR/TRN payment, N1 payer and payee), one CLP per
         claim, and one SVC per line with its adjustments, chosen so that every line
         balances: billed - adjustments = paid, where
           PR-1  deductible        = deductible_amount
           PR-2  coinsurance       = coinsurance_amount
           CO-45 contractual       = billed - paid - deductible - coinsurance
         A claim whose lines were all allowed nothing is denied (CLP02 = 4)

Everything is synthetic: the billing provider, its NPI-shaped id, the payer ids and the
trace numbers are invented, and members appear by member id only (no names). Synthea's
procedure codes are SNOMED, CDT, LOINC and CVX, which an 837P does not carry (5010 requires
CPT/HCPCS, qualifier HC); they are written under the mutually defined qualifier ZZ, and a
real clearinghouse would reject them. ISA15 is T (test data). The same input and date give
byte-identical output.

Usage: python packs/healthcare/generators/x12.py CLAIMS_CSV OUT_DIR [--date YYYYMMDD]
       writes OUT_DIR/claims837_<date>.x12 and OUT_DIR/remit835_<date>.x12
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

SENDER = "DIPSYNTHCLIN"
RECEIVER = "DIPSYNTHPAYER"
BILLING_NAME = "INVENTED CLINIC"
BILLING_NPI = "1999999984"
BILLING_TIN = "990000001"
VERSION_837 = "005010X222A1"
VERSION_835 = "005010X221A1"
ELEMENT, COMPONENT, TERMINATOR = "*", ":", "~"


def _digits(text: str, n: int) -> str:
    """n stable digits derived from text."""
    return str(int(hashlib.sha256(text.encode()).hexdigest(), 16) % 10**n).zfill(n)


def payer_id(payer: str) -> str:
    return "P" + _digits(payer, 5)


def _d8(iso: str) -> str:
    return iso.replace("-", "")


def _money(d: Decimal) -> str:
    return f"{d:.2f}"


def _isa(control: str, date8: str, time4: str, sender: str, receiver: str) -> str:
    fields = [
        "ISA",
        "00",
        " " * 10,
        "00",
        " " * 10,
        "ZZ",
        sender.ljust(15),
        "ZZ",
        receiver.ljust(15),
        date8[2:],
        time4,
        "^",
        "00501",
        control,
        "0",
        "T",
        COMPONENT,
    ]
    return ELEMENT.join(fields)


def _interchange(
    transactions: list[list[str]],
    functional: str,
    version: str,
    date8: str,
    control: int,
    sender: str,
    receiver: str,
) -> bytes:
    """ISA/GS ... GE/IEA around the transactions (each a list of segments, ST..SE)."""
    time4 = "1200"
    isa13 = str(control).zfill(9)
    segs = [
        _isa(isa13, date8, time4, sender, receiver),
        ELEMENT.join(
            ["GS", functional, sender, receiver, date8, time4, str(control), "X", version]
        ),
    ]
    for tx in transactions:
        segs += tx
    segs += [
        ELEMENT.join(["GE", str(len(transactions)), str(control)]),
        ELEMENT.join(["IEA", "1", isa13]),
    ]
    return ("".join(s + TERMINATOR + "\n" for s in segs)).encode("ascii")


def _transaction(st01: str, n: int, version: str, body: list[list[str]]) -> list[str]:
    control = str(n).zfill(4)
    segs = [["ST", st01, control, version], *body]
    segs.append(["SE", str(len(segs) + 1), control])
    return [ELEMENT.join(s) for s in segs]


def read_claims(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _by_payer(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        out[r["payer"]].append(r)
    return dict(sorted(out.items()))


def _claims(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: (r["claim_id"], int(r["line_number"]))):
        out[r["claim_id"]].append(r)
    return out


def x12_837(rows: list[dict[str, str]], date8: str, control: int = 1) -> bytes:
    transactions = []
    for n, (payer, lines) in enumerate(_by_payer(rows).items(), start=1):
        body = [
            ["BHT", "0019", "00", f"DIP{date8}{n:04d}", date8, "1200", "CH"],
            ["NM1", "41", "2", BILLING_NAME, "", "", "", "", "46", SENDER],
            ["PER", "IC", "SYNTHETIC DATA DESK", "TE", "5550100000"],
            ["NM1", "40", "2", payer.upper(), "", "", "", "", "46", payer_id(payer)],
            ["HL", "1", "", "20", "1"],
            ["NM1", "85", "2", BILLING_NAME, "", "", "", "", "XX", BILLING_NPI],
            ["N3", "1 INVENTED WAY"],
            ["N4", "SYNTHVILLE", "NY", "10001"],
            ["REF", "EI", BILLING_TIN],
        ]
        hl = 1
        members: dict[str, list[dict[str, str]]] = defaultdict(list)
        for r in lines:
            members[r["member_id"]].append(r)
        for member, member_lines in sorted(members.items()):
            hl += 1
            body += [
                ["HL", str(hl), "1", "22", "0"],
                ["SBR", "P", "18", "", "", "", "", "", "", "CI"],
                ["NM1", "IL", "1", "SUBSCRIBER", "SYNTHETIC", "", "", "", "MI", member],
                ["NM1", "PR", "2", payer.upper(), "", "", "", "", "PI", payer_id(payer)],
            ]
            for claim_id, claim_lines in _claims(member_lines).items():
                total = sum((Decimal(r["billed_amount"]) for r in claim_lines), Decimal("0"))
                pos = claim_lines[0]["place_of_service"]
                body.append(
                    [
                        "CLM",
                        claim_id,
                        _money(total),
                        "",
                        "",
                        COMPONENT.join([pos, "B", "1"]),
                        "Y",
                        "A",
                        "Y",
                        "Y",
                    ]
                )
                for r in claim_lines:
                    body += [
                        ["LX", r["line_number"]],
                        [
                            "SV1",
                            COMPONENT.join(["ZZ", r["procedure_code"]]),
                            _money(Decimal(r["billed_amount"])),
                            "UN",
                            "1",
                            "",
                            "",
                            "1",
                        ],
                        ["DTP", "472", "D8", _d8(r["service_date"])],
                        ["REF", "6R", r["claim_line_id"]],
                    ]
        transactions.append(_transaction("837", n, VERSION_837, body))
    return _interchange(transactions, "HC", VERSION_837, date8, control, SENDER, RECEIVER)


def _adjustments(r: dict[str, str]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    billed, paid = Decimal(r["billed_amount"]), Decimal(r["paid_amount"])
    deductible, coinsurance = Decimal(r["deductible_amount"]), Decimal(r["coinsurance_amount"])
    return paid, billed - paid - deductible - coinsurance, deductible, coinsurance


def x12_835(rows: list[dict[str, str]], date8: str, control: int = 2) -> bytes:
    transactions = []
    for n, (payer, lines) in enumerate(_by_payer(rows).items(), start=1):
        claims = _claims(lines)
        total_paid = sum((Decimal(r["paid_amount"]) for r in lines), Decimal("0"))
        trace = f"EFT{date8}{_digits(payer, 6)}"
        body = [
            [
                "BPR",
                "I",
                _money(total_paid),
                "C",
                "ACH",
                "CCP",
                "01",
                "999999992",
                "DA",
                "000000000",
                "1" + _digits(payer, 9),
                "",
                "01",
                "999999992",
                "DA",
                "000000001",
                date8,
            ],
            ["TRN", "1", trace, "1" + _digits(payer, 9)],
            ["DTM", "405", date8],
            ["N1", "PR", payer.upper()],
            ["N3", "1 INVENTED PLAZA"],
            ["N4", "SYNTHVILLE", "NY", "10001"],
            ["N1", "PE", BILLING_NAME, "XX", BILLING_NPI],
            ["LX", "1"],
        ]
        for claim_id, claim_lines in claims.items():
            billed = sum((Decimal(r["billed_amount"]) for r in claim_lines), Decimal("0"))
            paid = sum((Decimal(r["paid_amount"]) for r in claim_lines), Decimal("0"))
            patient = sum(
                (
                    Decimal(r["deductible_amount"]) + Decimal(r["coinsurance_amount"])
                    for r in claim_lines
                ),
                Decimal("0"),
            )
            denied = all(Decimal(r["allowed_amount"]) == 0 for r in claim_lines)
            body += [
                [
                    "CLP",
                    claim_id,
                    "4" if denied else "1",
                    _money(billed),
                    _money(paid),
                    _money(patient),
                    "15",
                    "PCN" + _digits(claim_id, 10),
                    claim_lines[0]["place_of_service"],
                    "1",
                ],
                [
                    "NM1",
                    "QC",
                    "1",
                    "SUBSCRIBER",
                    "SYNTHETIC",
                    "",
                    "",
                    "",
                    "MI",
                    claim_lines[0]["member_id"],
                ],
            ]
            for r in claim_lines:
                line_paid, contractual, deductible, coinsurance = _adjustments(r)
                body += [
                    [
                        "SVC",
                        COMPONENT.join(["ZZ", r["procedure_code"]]),
                        _money(Decimal(r["billed_amount"])),
                        _money(line_paid),
                        "",
                        "1",
                    ],
                    ["DTM", "472", _d8(r["service_date"])],
                ]
                if contractual:
                    body.append(["CAS", "CO", "45", _money(contractual)])
                pr = []
                if deductible:
                    pr += ["1", _money(deductible), ""]
                if coinsurance:
                    pr += ["2", _money(coinsurance), ""]
                if pr:
                    body.append(["CAS", "PR", *pr[:-1]])
                body.append(["REF", "6R", r["claim_line_id"]])
        transactions.append(_transaction("835", n, VERSION_835, body))
    return _interchange(transactions, "HP", VERSION_835, date8, control, RECEIVER, SENDER)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("claims_csv", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--date",
        default=datetime.now(UTC).strftime("%Y%m%d"),
        help="file date (default: today, UTC)",
    )
    args = parser.parse_args()
    rows = read_claims(args.claims_csv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (
        (f"claims837_{args.date}.x12", x12_837(rows, args.date)),
        (f"remit835_{args.date}.x12", x12_835(rows, args.date)),
    ):
        (args.out_dir / name).write_bytes(data)
        print(f"wrote {args.out_dir / name}: {data.count(TERMINATOR.encode())} segments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
