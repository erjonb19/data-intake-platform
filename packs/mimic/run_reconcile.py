"""M3: reconcile OMOP built from the MIMIC-IV FHIR demo (a) and the relational demo (b).

    python packs/mimic/run_reconcile.py --fhir-dsn postgresql://.../mimic_fhir \\
                                        --rel-dsn  postgresql://.../mimic_rel

Run after packs/mimic/run_fhir_demo.py and run_relational_demo.py. REAL, de-identified
data stays in the two local warehouses; the report (data/mimic/reports/, local) holds only
aggregate counts and difference descriptors, never values.

Every difference is classified by a rule below. A difference no rule covers is reported as
"unexplained", so a new kind of difference cannot hide inside an old explanation.
"""

from __future__ import annotations

import argparse
import json
import os

from dip import reconcile
from dip.contracts import REPO_ROOT

REPORTS = REPO_ROOT / "data/mimic/reports"

# Rows found on one side only.
ONE_SIDED = {
    ("visit", "a", 9203): "ED visit: the FHIR demo includes the MIMIC-IV-ED demo; the "
    "relational demo has no ED module",
    ("condition", "a", 9203): "ED diagnosis (MIMIC-IV-ED demo), recorded on an ED visit",
}

# Attribute differences on matching rows: (domain, attribute, descriptor) -> explanation.
ATTRIBUTES = {
    ("person", "race_concept_id", "-8527"): "Hispanic/Latino race text: the FHIR export assigns "
    "race White; the relational mapping records ethnicity Hispanic and leaves race unknown",
    ("measurement", "unit", "null on a"): "text-only result: FHIR carries it as valueString, "
    "which has no unit field, so the relational valueuom has no FHIR equivalent",
    ("measurement", "value", "null on b"): "comparator result (for example '<5'): the FHIR "
    "export parses the number, the relational valuenum is empty",
}


def explain(side: str, domain: str, row: dict) -> str:
    concept = row.get("visit_concept_id")
    return ONE_SIDED.get((domain, side, concept), "unexplained")


def _text_only_unit_rows(fhir_rows: list[dict], rel_rows: list[dict]) -> tuple[int, int]:
    """(unit differences, of which text-only on both sides). The unit explanation holds
    only if the two numbers are equal; otherwise the rest counts as unexplained."""
    by_key: dict[tuple, list[dict]] = {}
    for r in rel_rows:
        by_key.setdefault((r["subject"], r["item"], r["at"]), []).append(r)
    total = text_only = 0
    for r in fhir_rows:
        if r["unit"] is not None:
            continue
        other = next(
            (o for o in by_key.get((r["subject"], r["item"], r["at"]), []) if o["unit"]), None
        )
        if other is not None:
            total += 1
            text_only += r["value"] is None and other["value"] is None
    return total, text_only


def run(fhir_conn, rel_conn) -> dict:
    report: dict = {}
    fm = reconcile.fetch(fhir_conn, "measurement")
    rm = reconcile.fetch(rel_conn, "measurement")
    unit_total, unit_text_only = _text_only_unit_rows(fm, rm)
    for domain in reconcile.DOMAINS:
        r = reconcile.compare(
            domain, reconcile.fetch(fhir_conn, domain), reconcile.fetch(rel_conn, domain), explain
        )
        attrs = {}
        for attr, kinds in r.attr_diff_kinds.items():
            attrs[attr] = {
                kind: {
                    "rows": n,
                    "explanation": ATTRIBUTES.get((domain, attr, kind), "unexplained"),
                }
                for kind, n in kinds.most_common()
            }
        report[domain] = {
            "rows_fhir": r.rows_a,
            "rows_relational": r.rows_b,
            "matched": r.matched,
            "attribute_differences": attrs,
            "only_fhir": dict(r.only_a_by_reason),
            "only_relational": dict(r.only_b_by_reason),
        }
    # The unit rule is verified, not assumed: unit differences that are not text-only
    # results move to "unexplained".
    unit = report["measurement"]["attribute_differences"].get("unit", {}).get("null on a")
    if unit is not None:
        unit["verified_text_only"] = unit_text_only
        if unit_text_only != unit_total:
            report["measurement"]["attribute_differences"]["unit"]["not text-only"] = {
                "rows": unit_total - unit_text_only,
                "explanation": "unexplained",
            }
            unit["rows"] = unit_text_only
    report["unexplained"] = sum(
        n
        for d in report.values()
        for n in (
            [v for k, v in d["only_fhir"].items() if k == "unexplained"]
            + [v for k, v in d["only_relational"].items() if k == "unexplained"]
            + [
                x["rows"]
                for kinds in d["attribute_differences"].values()
                for x in kinds.values()
                if x["explanation"] == "unexplained"
            ]
        )
    )
    return report


def main() -> int:
    import psycopg

    parser = argparse.ArgumentParser()
    parser.add_argument("--fhir-dsn", default=os.environ.get("MIMIC_FHIR_DSN"))
    parser.add_argument("--rel-dsn", default=os.environ.get("MIMIC_REL_DSN"))
    args = parser.parse_args()
    if not (args.fhir_dsn and args.rel_dsn):
        raise SystemExit("--fhir-dsn and --rel-dsn are required")
    with psycopg.connect(args.fhir_dsn) as a, psycopg.connect(args.rel_dsn) as b:
        report = run(a, b)
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / "reconciliation_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nreport (local only, aggregates): {out}")
    return 0 if report["unexplained"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
