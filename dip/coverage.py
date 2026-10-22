"""Vocabulary coverage: how many OMOP clinical rows have no standard concept (concept_id 0).

Reports counts and rates only, never source values, so the output is safe to publish for
any source (including the MIMIC-IV demos).
"""

from __future__ import annotations

from typing import Any

# (OMOP table, concept column, source value column)
DOMAINS = (
    ("condition_occurrence", "condition_concept_id", "condition_source_value"),
    ("observation", "observation_concept_id", "observation_source_value"),
    ("measurement", "measurement_concept_id", "measurement_source_value"),
    ("drug_exposure", "drug_concept_id", "drug_source_value"),
)


def unmapped(conn: Any, schema: str = "omop") -> dict[str, dict[str, Any]]:
    """Per table: rows, unmapped rows, distinct source codes, unmapped distinct codes."""
    out: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        for table, concept, source in DOMAINS:
            cur.execute(
                f"select count(*), count(*) filter (where {concept} = 0),"
                f" count(distinct {source}),"
                f" count(distinct {source}) filter (where {concept} = 0)"
                f" from {schema}.{table}"
            )
            rows, rows_unmapped, codes, codes_unmapped = cur.fetchone()
            out[table] = {
                "rows": rows,
                "rows_unmapped": rows_unmapped,
                "rows_unmapped_pct": round(100 * rows_unmapped / rows, 1) if rows else None,
                "codes": codes,
                "codes_unmapped": codes_unmapped,
                "codes_unmapped_pct": round(100 * codes_unmapped / codes, 1) if codes else None,
            }
    conn.commit()
    return out


def format_table(results: dict[str, dict[str, dict[str, Any]]]) -> str:
    """Markdown table: one row per (label, OMOP table)."""
    lines = [
        "| build | table | rows | unmapped rows | distinct codes | unmapped codes |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for label, tables in results.items():
        for table, r in tables.items():
            if not r["rows"]:
                continue
            lines.append(
                f"| {label} | {table} | {r['rows']:,} | {r['rows_unmapped']:,}"
                f" ({r['rows_unmapped_pct']}%) | {r['codes']:,} | {r['codes_unmapped']:,}"
                f" ({r['codes_unmapped_pct']}%) |"
            )
    return "\n".join(lines)
