"""Reconcile OMOP built from two sources of the same patients (M3).

Each side is a warehouse where OMOP was built (for MIMIC: one from the FHIR demo, one from
the relational demo). Rows are compared by the *source's* keys, carried through the
staging layer (subject_id, hadm_id, ICD code, lab item and time), never by OMOP ids,
which are hashes of different source ids on each side.

For every domain the result has:
  matched      rows identical on both sides (key and attributes)
  attr_diff    rows whose key matches but an attribute differs, counted per attribute
  only_a/only_b rows whose key exists on one side only
Rows are multisets: two identical lab results at the same minute count twice.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# OMOP ids are omop_id(<staging id>), the dbt macro; the same expression in plain SQL.
_ID = "('x' || substr(md5({}), 1, 15))::bit(60)::bigint"

_PEOPLE = """
    select p.source_identifier as subject, p.patient_id
    from silver.stg_patient p
"""

# The source key of a row's visit (hadm_id, or the ED stay id), when it has one.
_VISIT_KEY = "left join silver.stg_encounter e on v.visit_occurrence_id = " + _ID.format(
    "e.encounter_id"
)

DOMAINS: dict[str, dict[str, Any]] = {
    "person": {
        "key": ["subject"],
        "attrs": ["gender_concept_id", "year_of_birth", "race_concept_id", "ethnicity_concept_id"],
        "sql": f"""
            select pp.subject, o.gender_concept_id, o.year_of_birth, o.race_concept_id,
                o.ethnicity_concept_id
            from omop.person o join ({_PEOPLE}) pp on o.person_id = {_ID.format("pp.patient_id")}
        """,
    },
    "visit": {
        "key": ["subject", "visit_key"],
        "attrs": ["visit_concept_id", "start", "end"],
        "sql": f"""
            select pp.subject, e.source_identifier as visit_key, v.visit_concept_id,
                v.visit_start_datetime::text as start, v.visit_end_datetime::text as "end"
            from omop.visit_occurrence v
            join silver.stg_encounter e on v.visit_occurrence_id = {_ID.format("e.encounter_id")}
            join ({_PEOPLE}) pp on v.person_id = {_ID.format("pp.patient_id")}
        """,
    },
    "condition": {
        "key": ["subject", "visit_key", "code"],
        "attrs": ["start_date", "visit_concept_id"],
        "sql": f"""
            select pp.subject, e.source_identifier as visit_key, c.condition_source_value as code,
                c.condition_start_date::text as start_date, v.visit_concept_id
            from omop.condition_occurrence c
            join ({_PEOPLE}) pp on c.person_id = {_ID.format("pp.patient_id")}
            left join omop.visit_occurrence v on v.visit_occurrence_id = c.visit_occurrence_id
            {_VISIT_KEY}
        """,
    },
    "measurement": {
        "key": ["subject", "item", "at"],
        "attrs": ["value", "unit", "visit_key"],
        "sql": f"""
            select pp.subject, m.measurement_source_value as item,
                m.measurement_datetime::text as at, m.value_as_number::text as value,
                m.unit_source_value as unit, e.source_identifier as visit_key
            from omop.measurement m
            join ({_PEOPLE}) pp on m.person_id = {_ID.format("pp.patient_id")}
            left join omop.visit_occurrence v on v.visit_occurrence_id = m.visit_occurrence_id
            {_VISIT_KEY}
        """,
    },
}


@dataclass
class DomainResult:
    rows_a: int = 0
    rows_b: int = 0
    matched: int = 0
    attr_diff: Counter = field(default_factory=Counter)
    only_a: int = 0
    only_b: int = 0
    # Rows on one side only, grouped by a caller-supplied explanation.
    only_a_by_reason: Counter = field(default_factory=Counter)
    only_b_by_reason: Counter = field(default_factory=Counter)
    # How attributes differ, as aggregate descriptors (never raw values): for example
    # {"start": {"+5h": 12}} or {"year_of_birth": {"+1": 30}}.
    attr_diff_kinds: dict = field(default_factory=dict)


def describe(a: Any, b: Any) -> str:
    """How b differs from a, without revealing either value."""
    if a is None or b is None:
        return "null on " + ("a" if a is None else "b")
    try:
        ta, tb = datetime.fromisoformat(str(a)), datetime.fromisoformat(str(b))
        hours = (tb - ta).total_seconds() / 3600
        return f"{hours:+g}h"
    except ValueError:
        pass
    try:
        return f"{float(b) - float(a):+g}"
    except ValueError:
        return "changed"


def fetch(conn: Any, domain: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(DOMAINS[domain]["sql"])
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
    conn.commit()
    return rows


def compare(domain: str, a: list[dict], b: list[dict], explain=None) -> DomainResult:
    """`explain(side, row)` returns a reason string for a row found on one side only."""
    spec = DOMAINS[domain]
    key = lambda r: tuple(r[k] for k in spec["key"])  # noqa: E731
    full = lambda r: key(r) + tuple(r[k] for k in spec["attrs"])  # noqa: E731
    res = DomainResult(rows_a=len(a), rows_b=len(b))
    fa, fb = Counter(map(full, a)), Counter(map(full, b))
    res.matched = sum((fa & fb).values())
    # Remove exact matches, then pair the rest by key.
    rest_a = list((fa - fb).elements())
    rest_b = list((fb - fa).elements())
    by_key_b: dict[tuple, list[tuple]] = {}
    for t in rest_b:
        by_key_b.setdefault(t[: len(spec["key"])], []).append(t)
    unmatched_a = []
    nkey = len(spec["key"])
    for t in rest_a:
        candidates = by_key_b.get(t[:nkey])
        if candidates:
            other = candidates.pop()
            for i, attr in enumerate(spec["attrs"]):
                if t[nkey + i] != other[nkey + i]:
                    res.attr_diff[attr] += 1
                    kinds = res.attr_diff_kinds.setdefault(attr, Counter())
                    kinds[describe(t[nkey + i], other[nkey + i])] += 1
        else:
            unmatched_a.append(t)
    unmatched_b = [t for ts in by_key_b.values() for t in ts]
    res.only_a, res.only_b = len(unmatched_a), len(unmatched_b)
    names = spec["key"] + spec["attrs"]
    for side, rows_, counter in (
        ("a", unmatched_a, res.only_a_by_reason),
        ("b", unmatched_b, res.only_b_by_reason),
    ):
        for t in rows_:
            row = dict(zip(names, t, strict=True))
            counter[explain(side, domain, row) if explain else "unexplained"] += 1
    return res
