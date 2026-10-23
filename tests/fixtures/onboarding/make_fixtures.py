"""Writes the onboarding fixtures for the contract-author evals (Stage 5):
python tests/fixtures/onboarding/make_fixtures.py

Three new vendors' first files, all invented and generated with a fixed seed (no Faker), so
rerunning gives byte-identical files: a lab results CSV, a provider roster CSV, and a
pharmacy claims Excel workbook. Their reference contracts, written by hand, are in
agents/evals/contract_author/reference/.
"""

from __future__ import annotations

import io
import random
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
FIRST = ["Alex", "Sam", "Jordan", "Riley", "Casey", "Morgan", "Taylor", "Jamie", "Quinn"]
LAST = ["Example", "Sample", "Placeholder", "Fixture", "Testcase", "Demo", "Mock"]
DAY = date(2026, 10, 19)


def lab_results(rng: random.Random, n: int = 30) -> str:
    tests = [("2345-7", "mg/dL", 70, 180), ("4548-4", "%", 4.5, 9.5),
             ("2160-0", "mg/dL", 0.5, 2.5), ("718-7", "g/dL", 10, 17)]  # fmt: skip
    lines = ["result_id,patient_ref,collected_date,loinc_code,result_value,unit,abnormal_flag,"
             "ordering_provider"]  # fmt: skip
    for i in range(n):
        code, unit, lo, hi = rng.choice(tests)
        value = round(rng.uniform(lo * 0.8, hi * 1.2), 1)
        flag = "H" if value > hi else "L" if value < lo else "N"
        when = DAY - timedelta(days=rng.randrange(0, 30))
        lines.append(f"LR{100000 + i},pt-{rng.randrange(1000, 9999)},{when},{code},{value},"
                     f"{unit},{flag},{rng.choice(FIRST)} {rng.choice(LAST)}")  # fmt: skip
    return "\n".join(lines) + "\n"


def provider_roster(rng: random.Random, n: int = 24) -> str:
    specialties = ["207Q00000X", "207R00000X", "208D00000X", "363L00000X"]
    states = ["NY", "NJ", "CT"]
    lines = ["npi,provider_first_name,provider_last_name,taxonomy_code,practice_state,"
             "accepting_new_patients,effective_date"]  # fmt: skip
    for _ in range(n):
        npi = f"1{rng.randrange(10**8, 10**9)}"
        start = date(2020, 1, 1) + timedelta(days=rng.randrange(0, 2000))
        lines.append(f"{npi},{rng.choice(FIRST)},{rng.choice(LAST)},{rng.choice(specialties)},"
                     f"{rng.choice(states)},{rng.choice(['Y', 'N'])},{start}")  # fmt: skip
    return "\n".join(lines) + "\n"


def pharmacy_claims(rng: random.Random, n: int = 25) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Claims"
    ws.append(["rx_claim_id", "member_id", "fill_date", "ndc", "days_supply", "quantity",
               "paid_amount", "pharmacy_npi"])  # fmt: skip
    for i in range(n):
        ws.append([
            f"RX{700000 + i}", f"M{rng.randrange(10**8, 10**9):09d}",
            DAY - timedelta(days=rng.randrange(0, 30)),
            f"{rng.randrange(10**10, 10**11):011d}", rng.choice([30, 60, 90]),
            rng.choice([30, 60, 90, 120]), round(rng.uniform(5, 400), 2),
            f"1{rng.randrange(10**8, 10**9)}",
        ])  # fmt: skip
    fixed = datetime(2026, 1, 1)
    wb.properties.created = wb.properties.modified = fixed
    buf = io.BytesIO()
    wb.save(buf)
    return _deterministic(buf.getvalue(), fixed)


def _deterministic(data: bytes, when: datetime) -> bytes:
    """The finance generator's fix: fixed zip entry dates and docProps `modified` time."""
    import importlib.util

    path = HERE.parents[2] / "packs/finance/generators/invoices.py"
    spec = importlib.util.spec_from_file_location("invoices_gen", path)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    gen.FIXED = when
    return gen._deterministic(data)


def eligibility_base(rng: random.Random, n: int = 24) -> str:
    """A clean eligibility file (the feed's contract), the base the drift cases mutate."""
    lines = ["member_id,first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,"
             "patient_ref"]  # fmt: skip
    for i in range(n):
        dob = date(1940, 1, 1) + timedelta(days=rng.randrange(0, 25000))
        start = date(2022, 1, 1) + timedelta(days=rng.randrange(0, 1200))
        end = "" if rng.random() < 0.7 else str(start + timedelta(days=365))
        lines.append(f"M{i + 1:09d},{rng.choice(FIRST)},{rng.choice(LAST)},{dob},"
                     f"{rng.choice('FMU')},{rng.choice(['HMO', 'PPO', 'EPO', 'HDHP'])},"
                     f"{start},{end},ref-{i + 1:04d}")  # fmt: skip
    return "\n".join(lines) + "\n"


def main() -> None:
    files = {
        "lab_results_20261019.csv": lab_results(random.Random(51)).encode(),
        "provider_roster_20261019.csv": provider_roster(random.Random(52)).encode(),
        "pharmacy_claims_20261019.xlsx": pharmacy_claims(random.Random(53)),
        "elig_base.csv": eligibility_base(random.Random(54)).encode(),
    }
    for name, data in files.items():
        (HERE / name).write_bytes(data)
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
