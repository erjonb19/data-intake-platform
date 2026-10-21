from __future__ import annotations

import os
from pathlib import Path

from dip import vocab

# The header of mimic-code's d_labitems_to_loinc.csv (v2.4.0), with invented rows.
LAB_MAP = (
    "itemid (omop_source_code),label,fluid,category,valueuom,omop_concept_id,"
    "omop_concept_name,omop_domain_id,omop_vocabulary_id\n"
    "99001,Invented lab A,Blood,Chemistry,mg/dL,40762125,Lactate,Measurement,LOINC\n"
    "99002,Invented lab B,Blood,Chemistry,,0,No matching concept,Metadata,None\n"
    "99003,Invented lab C,Blood,Chemistry,,,,,\n"
    "99004,Invented lab D,Blood,Chemistry,,12345-6,A code in the id column,Measurement,LOINC\n"
)


def test_mimic_lab_map_keeps_mapped_items_only():
    assert vocab.read_mimic_lab_map(LAB_MAP.encode()) == [
        ("99001", 0, "MIMIC_LAB", "Invented lab A", 40762125, "LOINC", "1970-01-01",
         "2099-12-31", None),
    ]  # fmt: skip


def test_athena_dirs_from_env():
    value = os.pathsep.join(["a", "", "b"])
    assert vocab.athena_dirs(value) == [Path("a"), Path("b")]
    assert vocab.athena_dirs("") == []
