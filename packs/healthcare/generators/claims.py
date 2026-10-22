"""Generate a synthetic payer claims file from Synthea patients (Stage 2, S2.4).

One row per billed claim line, taken from the ExplanationOfBenefit resources already in
the Synthea FHIR R4 bundles (synthea.sh), in the layout of the `healthcare.claims`
contract. Only lines with an adjudication (an amount) are billed lines, and only lines
from the plan's start (2020-01-01, as in eligibility.py) are the payer's: earlier history
predates the plan. Everything is derived deterministically from the bundles, so the same
bundles always give a byte-identical file:

  claim_line_id     <claim_id>-<line_number>
  claim_id          the id of the Claim the EOB adjudicates (Synthea's)
  line_number       the EOB item's sequence
  member_id         as eligibility.py derives it from the Synthea patient id
  service_date      the item's servicedPeriod start (UTC date)
  place_of_service  the item's CMS place-of-service code
  procedure_code    the item's productOrService code
  procedure_system  SNOMED, CDT (dental), LOINC (lab tests), CVX (vaccines), else LOCAL
  billed_amount, allowed_amount, paid_amount, coinsurance_amount, deductible_amount
                    the Blue Button line adjudications (submitted, allowed, provider
                    payment, coinsurance, Part B deductible), USD with 2 decimals
  payer             the focal insurance's display name

All output is synthetic (Synthea, Apache-2.0).

Usage: python packs/healthcare/generators/claims.py BUNDLE_DIR OUT_DIR [--date YYYYMMDD]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

COLUMNS = (
    "claim_line_id",
    "claim_id",
    "line_number",
    "member_id",
    "service_date",
    "place_of_service",
    "procedure_code",
    "procedure_system",
    "billed_amount",
    "allowed_amount",
    "paid_amount",
    "coinsurance_amount",
    "deductible_amount",
    "payer",
)
PLAN_START = "2020-01-01"
BB = "https://bluebutton.cms.gov/resources/variables/"
AMOUNTS = {
    "billed_amount": BB + "line_sbmtd_chrg_amt",
    "allowed_amount": BB + "line_alowd_chrg_amt",
    "paid_amount": BB + "line_prvdr_pmt_amt",
    "coinsurance_amount": BB + "line_coinsrnc_amt",
    "deductible_amount": BB + "line_bene_ptb_ddctbl_amt",
}
SYSTEMS = {
    "http://snomed.info/sct": "SNOMED",
    "http://www.ada.org/cdt": "CDT",
    "http://loinc.org": "LOINC",  # billed lab tests
    "http://hl7.org/fhir/sid/cvx": "CVX",  # vaccines
}


def member_id(patient_id: str) -> str:
    """The same id eligibility.py gives this patient (a unit test keeps them equal)."""
    digest = int(hashlib.sha256(patient_id.encode("utf-8")).hexdigest(), 16)
    return f"M{digest % 10**9:09d}"


def _ref_id(reference: str) -> str:
    return reference.removeprefix("urn:uuid:")


def claim_rows(bundle_dir: Path) -> list[dict[str, str]]:
    rows = []
    for path in sorted(bundle_dir.glob("*.json")):
        bundle = json.loads(path.read_text("utf-8"))
        for entry in bundle.get("entry", []):
            eob = entry.get("resource", {})
            if eob.get("resourceType") != "ExplanationOfBenefit":
                continue
            claim_id = _ref_id(eob["claim"]["reference"])
            member = member_id(_ref_id(eob["patient"]["reference"]))
            focal = next((i for i in eob.get("insurance", []) if i.get("focal")), None)
            payer = ((focal or {}).get("coverage") or {}).get("display", "")
            for item in eob.get("item", []):
                adjudications = {
                    a["category"]["coding"][0]["code"]: a.get("amount", {}).get("value", 0.0)
                    for a in item.get("adjudication", [])
                }
                if not adjudications:
                    continue  # not a billed line
                service_date = item["servicedPeriod"]["start"][:10]
                if service_date < PLAN_START:
                    continue
                coding = item["productOrService"]["coding"][0]
                place = (item.get("locationCodeableConcept") or {}).get("coding", [{}])[0]
                rows.append(
                    {
                        "claim_line_id": f"{claim_id}-{item['sequence']}",
                        "claim_id": claim_id,
                        "line_number": str(item["sequence"]),
                        "member_id": member,
                        "service_date": service_date,
                        "place_of_service": place.get("code", ""),
                        "procedure_code": coding["code"],
                        "procedure_system": SYSTEMS.get(coding.get("system", ""), "LOCAL"),
                        **{
                            col: f"{float(adjudications.get(code, 0.0)):.2f}"
                            for col, code in AMOUNTS.items()
                        },
                        "payer": payer,
                    }
                )
    rows.sort(key=lambda r: (r["claim_id"], int(r["line_number"])))
    return rows


def claims_csv(bundle_dir: Path) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(claim_rows(bundle_dir))
    return out.getvalue().encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--date", default="20260101", help="file date (default: the Synthea reference date)"
    )
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"claims_{args.date}.csv"
    rows = claim_rows(args.bundle_dir)
    out.write_bytes(claims_csv(args.bundle_dir))
    print(f"wrote {out}: {len(rows)} claim lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
