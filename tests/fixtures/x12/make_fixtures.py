"""Write the X12 fixtures (Stage 4) from the invented claims fixture, deterministically.

    python tests/fixtures/x12/make_fixtures.py

  good/claims837_good.x12       the 5 lines of ../claims/good/claims_good.csv as an 837P
  good/remit835_good.x12        an 835 paying 4 of them (the vaccine line is not remitted)
  bad/claims837_segment_lost.x12  the 837P with its first PER segment lost in transfer:
                                X12.ENVELOPE:SE01 (`dip break x12_segment_lost`)
  bad/claims837_is_a_835.x12    a remittance sent to the claims feed: X12.TRANSACTION:835
  bad/remit835_bad_date.x12     the 835 with one service date that is not a date: 1 line
                                in 4 fails VALUE.TYPE:service_date, over the contract's
                                max_bad_row_ratio, so the file is quarantined whole

Every value is invented (see ../claims/README.md); ISA15 marks the files as test data.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DATE = "20261021"


def _generator():
    spec = importlib.util.spec_from_file_location(
        "x12_gen", ROOT / "packs/healthcare/generators/x12.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixtures() -> dict[str, bytes]:
    import sys

    sys.path.insert(0, str(ROOT))
    from dip.breakages import x12_segment_lost

    gen = _generator()
    claims = gen.read_claims(ROOT / "tests/fixtures/claims/good/claims_good.csv")
    remitted = [r for r in claims if r["procedure_system"] != "CVX"]
    remit = gen.x12_835(remitted, DATE)
    bad_date = remit.replace(b"DTM*472*20250610~", b"DTM*472*20251350~", 1)
    assert bad_date != remit
    return {
        "good/claims837_good.x12": gen.x12_837(claims, DATE),
        "good/remit835_good.x12": remit,
        "bad/claims837_segment_lost.x12": x12_segment_lost(gen.x12_837(claims, DATE)),
        "bad/claims837_is_a_835.x12": gen.x12_835(claims, DATE),
        "bad/remit835_bad_date.x12": bad_date,
    }


def main() -> None:
    for name, data in fixtures().items():
        path = HERE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {path.relative_to(ROOT)} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
