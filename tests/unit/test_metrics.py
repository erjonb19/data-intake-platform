"""The warehouse metrics exporter's exposition text (S3.2), with a stand-in for Postgres."""

from __future__ import annotations

import re

from dip.metrics import LABELS, QUERIES, render

LINE = re.compile(r'^[a-z0-9_]+\{([a-z_]+="(?:[^"\\]|\\.)*",?)*\} -?[0-9.e+]+$')


def fake(results: dict[str, list[tuple]]):
    def run(sql: str):
        for name, (_, _, q) in QUERIES.items():
            if q == sql:
                if name not in results:
                    raise RuntimeError(f"relation for {name} does not exist")
                return results[name]
        raise AssertionError(sql)

    return run


def test_every_query_has_labels_for_all_but_its_value_column():
    assert set(QUERIES) == set(LABELS)
    for name, (_, _, sql) in QUERIES.items():
        # a read: a select, or (S11.4) a select after a WITH of selects
        assert sql.lower().startswith(("select", "with")), name
        assert not any(w in sql.lower() for w in ("insert ", "update ", "delete ", " drop ")), name


def test_series_are_well_formed_and_values_numeric():
    text = render(fake({
        "dip_intake_files": [("finance.vendor_invoice", "loaded", 3)],
        "dip_feed_last_received_timestamp_seconds": [("finance.vendor_invoice", 1.7e9)],
        "dip_rows_24h": [("finance.vendor_invoice", "accepted", None)],
    }))  # fmt: skip
    series = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert all(LINE.match(ln) for ln in series), series
    assert 'dip_intake_files{feed="finance.vendor_invoice",status="loaded"} 3.0' in series
    assert 'dip_rows_24h{feed="finance.vendor_invoice",outcome="accepted"} 0' in series


def test_a_failing_query_is_reported_and_does_not_hide_the_others():
    text = render(fake({"dip_intake_files": [("f", "loaded", 1)]}))
    assert 'dip_metrics_query_ok{query="dip_intake_files"} 1' in text
    assert 'dip_metrics_query_ok{query="dip_incidents_open"} 0' in text
    assert "# TYPE dip_incidents_open" not in text


def test_label_values_are_escaped():
    text = render(fake({"dip_quarantine_open": [('f"1', "A\\B\nC", 2)]}))
    assert 'dip_quarantine_open{feed="f\\"1",reason_code="A\\\\B\\nC"} 2.0' in text
