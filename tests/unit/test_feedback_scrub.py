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


def test_a_loose_pattern_keeps_no_original_letters():
    """A value matching a loose pattern is replaced whole, not only in its digits."""
    from dip.agents.feedback import Rule

    rule = Rule.of({"name": "procedure_code", "pattern": "^[A-Za-z0-9.-]{1,20}$"})
    s = Scrubber(5)
    new = s.value("OAKST-12", rule)
    assert rule.pattern.fullmatch(new) and new != "OAKST-12"
    assert "OAKST" not in new


def test_a_value_is_memoised_per_column_and_never_lands_on_an_allowed_one():
    from dip.agents.feedback import Rule

    s = Scrubber(1)
    free, sex = Rule.of({"name": "note"}), Rule.of({"name": "sex", "allowed": ["F", "M", "U"]})
    for _ in range(50):  # whatever "X" became in a free column, in `sex` it stays not allowed
        s.value("X", free)
    assert s.value("X", sex) not in {"F", "M", "U"}


def test_numbers_keep_their_side_of_min_and_max():
    from dip.agents.feedback import Rule

    rule = Rule.of({"name": "line_number", "type": "integer", "min": 1, "max": 99})
    s = Scrubber(2)
    assert float(s.value("0", rule)) < 1  # below the minimum stays below
    assert 1 <= float(s.value("42", rule)) <= 99


def test_a_unique_column_never_gets_two_values_the_same():
    from dip.agents.feedback import Rule

    rule = Rule.of({"name": "code", "unique": True})
    s = Scrubber(3)
    got = {s.value(str(d), rule) for d in range(10)}  # one digit each: few choices
    assert len(got) == 10


def test_without_a_header_or_contract_the_first_row_is_data():
    text = "Alex,Hallett,1980-02-14\nSam,Okafor,1975-07-30\n"
    out = scrub_csv(text, None, seed=1)
    assert "Alex" not in out and "Hallett" not in out


def test_file_names_keep_only_the_pattern_and_a_valid_date():
    from dip.agents.feedback import scrub_name

    c = SimpleNamespace(spec={"source": {"filename_pattern": "elig_*.csv"}})
    assert scrub_name("elig_20261020_DanaWhitfield.csv", c, 1) == "elig_20261020.csv"
    name = scrub_name("elig_M000000012.csv", c, 1)
    assert name.startswith("elig_") and "M000000012" not in name
    assert scrub_name("Robin Achterberg.csv", None, 1).startswith("sample_")
