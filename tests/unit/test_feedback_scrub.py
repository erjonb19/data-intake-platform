"""Stage 11 (S11.5): scrubbing a rejected proposal's files to synthetic values before they
become an eval case. Each cell keeps its shape with new characters, a valid date stays a
valid date, a contract's allowed value stays, the header stays, and no original value
with a letter or digit in it survives."""

from __future__ import annotations

import csv
import io
from datetime import date
from types import SimpleNamespace

from dip.agents.feedback import Scrubber, scrub_csv

SOURCE = (
    "member_id,first_name,last_name,dob,sex,plan_code,coverage_start,coverage_end,patient_ref\n"
    "M000000011,Robin,Achterberg,1984-05-09,F,HMO,2024-01-01,,\n"
    "M000000012,Dana,Whitfield,1979-08-17,X,PPO,2023-06-01,,ref-77\n"
    "M000000011,Robin,Achterberg,1984-02-30,F,GOLD,2024-01-01,,\n"
)
CONTRACT = SimpleNamespace(spec={"columns": [
    {"name": "sex", "allowed": ["F", "M", "U"]},
    {"name": "plan_code", "allowed": ["HMO", "PPO", "EPO", "HDHP"]},
]})  # fmt: skip


def cells(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_the_header_stays_and_every_vendor_value_is_replaced():
    out = cells(scrub_csv(SOURCE, CONTRACT, seed=7))
    src = cells(SOURCE)
    assert out[0] == src[0]
    allowed = {"F", "M", "U", "HMO", "PPO", "EPO", "HDHP"}
    originals = {v for row in src[1:] for v in row if any(c.isalnum() for c in v)} - allowed
    scrubbed = {v for row in out[1:] for v in row}
    assert not originals & scrubbed, originals & scrubbed


def test_shapes_dates_and_allowed_values():
    out = cells(scrub_csv(SOURCE, CONTRACT, seed=7))
    m1, m2, m3 = out[1], out[2], out[3]
    assert len(m1[0]) == 10 and m1[0][0].isupper() and m1[0][1:].isdigit()
    assert m1[1][0].isupper() and m1[1][1:].islower() and len(m1[1]) == len("Robin")
    date.fromisoformat(m1[3])  # a valid date stays valid
    # an invalid date keeps its shape (9999-99-99), never its value
    assert m3[3] != "1984-02-30" and len(m3[3]) == 10 and m3[3][4] == m3[3][7] == "-"
    assert (m1[4], m1[5]) == ("F", "HMO")  # the contract's allowed values stay
    assert m2[4] not in {"F", "M", "U"} and m3[5] not in {"HMO", "PPO", "EPO", "HDHP"}
    assert len(m2[8]) == 6 and m2[8][3] == "-" and m2[8] != "ref-77"
    assert m1[0] == m3[0] and m1[2] == m3[2]  # a repeated value repeats: keys still clash
    assert m1[7] == "" and m1[8] == ""  # empties stay empty


def test_the_same_seed_gives_the_same_case():
    assert scrub_csv(SOURCE, CONTRACT, seed=3) == scrub_csv(SOURCE, CONTRACT, seed=3)
    assert scrub_csv(SOURCE, CONTRACT, seed=3) != scrub_csv(SOURCE, CONTRACT, seed=4)


def test_values_without_letters_or_digits_are_kept():
    s = Scrubber(1)
    assert s.value("") == "" and s.value("--") == "--"
    assert s.value("Ünïcødé") != "Ünïcødé"
