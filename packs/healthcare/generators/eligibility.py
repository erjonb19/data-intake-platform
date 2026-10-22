"""Generate a synthetic payer eligibility file from Synthea patients (Stage 1).

One row per Synthea patient enrolled on or after the plan start (FHIR R4 bundles from
synthea.sh; patients who died before it, or lack a name or birth date, are left out
and counted), in the layout of the
`healthcare.eligibility` contract. Everything is derived deterministically from the
bundles, so the same bundles always give a byte-identical file:

  member_id       "M" + 9 digits from the sha256 of the Synthea patient id
  plan_code       one of HMO, PPO, EPO, HDHP, from the same hash
  coverage_start  the later of the birth date and the plan's start (2020-01-01)
  coverage_end    the date of death, if any; otherwise empty (open coverage)
  patient_ref     the Synthea patient id: the payer's link to the clinical record (FHIR/OMOP)

All output is synthetic (Synthea, Apache-2.0).

Usage: python packs/healthcare/generators/eligibility.py BUNDLE_DIR OUT_DIR [--date YYYYMMDD]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

COLUMNS = (
    "member_id",
    "first_name",
    "last_name",
    "dob",
    "sex",
    "plan_code",
    "coverage_start",
    "coverage_end",
    "patient_ref",
)
PLANS = ("HMO", "PPO", "EPO", "HDHP")
PLAN_START = "2020-01-01"
SEX = {"female": "F", "male": "M"}


def member_row(patient: dict) -> dict[str, str]:
    pid = patient["id"]
    digest = int(hashlib.sha256(pid.encode("utf-8")).hexdigest(), 16)
    name = next((n for n in patient.get("name", []) if n.get("use") == "official"), None)
    name = name or (patient.get("name") or [{}])[0]
    dob = patient["birthDate"]
    death = patient.get("deceasedDateTime", "")[:10]
    return {
        "member_id": f"M{digest % 10**9:09d}",
        "first_name": " ".join(name.get("given", [])),
        "last_name": name.get("family", ""),
        "dob": dob,
        "sex": SEX.get(patient.get("gender", ""), "U"),
        "plan_code": PLANS[(digest // 10**9) % len(PLANS)],
        "coverage_start": max(dob, PLAN_START),
        "coverage_end": death,
        "patient_ref": pid,
    }


def patients(bundle_dir: Path):
    for path in sorted(bundle_dir.glob("*.json")):
        bundle = json.loads(path.read_text("utf-8"))
        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            if resource.get("resourceType") == "Patient":
                yield resource


def eligibility_rows(bundle_dir: Path) -> tuple[list[dict[str, str]], dict[str, int]]:
    """(rows, counts of patients left out by reason)."""
    rows, left_out = [], {"no_birth_date": 0, "no_name": 0, "died_before_plan": 0}
    for p in patients(bundle_dir):
        if not p.get("birthDate"):
            left_out["no_birth_date"] += 1
            continue
        row = member_row(p)
        if not row["first_name"] or not row["last_name"]:
            left_out["no_name"] += 1
        # Patients who died before the plan started were never enrolled.
        elif row["coverage_end"] and row["coverage_end"] < row["coverage_start"]:
            left_out["died_before_plan"] += 1
        else:
            rows.append(row)
    ids = [r["member_id"] for r in rows]
    if len(ids) != len(set(ids)):
        # 9 hash digits: vanishingly rare, but never emit a file the contract would reject.
        raise ValueError("member_id collision; derive member ids with more digits")
    rows.sort(key=lambda r: r["member_id"])
    return rows, left_out


def eligibility_csv(bundle_dir: Path) -> bytes:
    rows, _ = eligibility_rows(bundle_dir)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
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
    out = args.out_dir / f"elig_{args.date}.csv"
    rows, left_out = eligibility_rows(args.bundle_dir)
    out.write_bytes(eligibility_csv(args.bundle_dir))
    print(f"wrote {out}: {len(rows)} members; left out: {left_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
