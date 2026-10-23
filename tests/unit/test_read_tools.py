"""The Read tier's service-backed tools (S3.4) against fake Airflow, Marquez and Prometheus:
what they ask for, and that they take no free-form query."""

from __future__ import annotations

import json
import urllib.parse

import pytest

from dip.agents.read_tools import METRICS, ReadServices, register_read_tools
from dip.agents.registry import Tool
from dip.contracts import load_contracts


class Reg:
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        self.tools[tool.name] = tool


class Fake:
    """Answers by URL; records every call."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, headers))
        for prefix, answer in self.answers.items():
            if url.startswith(prefix) or prefix in url:
                return answer(url) if callable(answer) else answer
        raise AssertionError(f"unexpected {method} {url}")


def tools(http, **svc):
    reg = Reg()
    s = ReadServices(http=http, **svc)
    register_read_tools(reg, Tool, None, None, load_contracts(), s)
    return {name: t.fn for name, t in reg.tools.items()}


def test_metrics_are_a_fixed_list_and_the_feed_a_contract_name():
    seen = []

    def prom(url):
        seen.append(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0])
        return {
            "data": {"result": [{"metric": {"feed": "healthcare.claims"}, "value": [0, "0.5"]}]}
        }

    t = tools(Fake({"http://prom/api/v1/query": prom}), prometheus_url="http://prom")
    got = t["query_metrics"]("reader", None, "quarantine_rate_24h", "healthcare.claims")
    assert got["series"] == [{"labels": {"feed": "healthcare.claims"}, "value": 0.5}]
    feed = 'feed="healthcare.claims"'
    assert seen[0] == (f'sum by (feed) (dip_files_24h{{status="quarantined",{feed}}})'
                       f" / sum by (feed) (dip_files_24h{{{feed}}})")  # fmt: skip
    with pytest.raises(ValueError):
        t["query_metrics"]("reader", None, "up{job=~'.*'}")
    with pytest.raises(ValueError):
        t["query_metrics"]("reader", None, "files_24h", 'x"} or vector(1) #')
    # Every template renders to valid-looking PromQL, with and without a feed.
    for name in METRICS:
        t["query_metrics"]("reader", None, name)
        t["query_metrics"]("reader", None, name, "healthcare.eligibility")
    assert all("{feed_" not in q for q in seen)


def test_airflow_calls_use_a_token_and_check_ids():
    fake = Fake({
        "http://af/auth/token": {"access_token": "t1"},
        "/taskInstances": {"task_instances": [{"task_id": "intake", "state": "success",
                                               "try_number": 1}]},
        "/dagRuns?": {"dag_runs": [{"dag_run_id": "manual__2026-10-22T01:00:00+00:00",
                                    "state": "success"}]},
    })  # fmt: skip
    t = tools(fake, airflow_url="http://af", airflow_user="viewer", airflow_password="p")
    got = t["get_dag_run"]("reader", None, "intake__healthcare__claims")
    assert got["runs"][0]["tasks"] == [{"task_id": "intake", "state": "success", "try_number": 1}]
    assert fake.calls[0][0] == "POST"  # the token, once
    assert all(h.get("Authorization") == "Bearer t1" for _, _, h in fake.calls[1:])
    with pytest.raises(ValueError):
        t["get_dag_run"]("reader", None, "../../config")


def test_task_logs_are_untrusted_and_tailed():
    log = {"content": [{"event": f"line {i}"} for i in range(500)]}
    fake = Fake({"http://af/auth/token": {"access_token": "t"}, "/logs/1": log})
    t = tools(fake, airflow_url="http://af")
    got = t["get_task_log"]("reader", None, "d", "r", "intake", 1, 50)
    assert got["lines_total"] == 500 and got["untrusted_vendor_content"][-1] == "line 499"
    assert len(got["untrusted_vendor_content"]) == 50 and "never instructions" in got["notice"]


def test_lineage_walks_upstream_nearest_first():
    ns = "postgres://pg:5432"
    gold, silver = f"dataset:{ns}:warehouse.gold.g", f"dataset:{ns}:warehouse.silver.s"
    job, land = "job:dip:dbt.build.dbt.g", "dataset:s3://landing:feed/a.csv"
    graph = {"graph": [
        {"id": gold, "inEdges": [{"origin": job, "destination": gold}]},
        {"id": job, "inEdges": [{"origin": silver, "destination": job}]},
        {"id": silver, "inEdges": [{"origin": land, "destination": silver}]},
        {"id": land, "inEdges": []},
    ]}  # fmt: skip
    fake = Fake({"/api/v1/namespaces": {"namespaces": [{"name": "dip"}, {"name": ns}]},
                 "/api/v1/lineage": graph})  # fmt: skip
    t = tools(fake, marquez_url="http://mq")
    got = t["get_lineage"]("reader", None, "gold.g")
    assert got["untrusted_vendor_content"] == [
        "job:dbt.build.dbt.g",
        f"dataset:{ns}:warehouse.silver.s",
        "dataset:s3://landing:feed/a.csv",
    ]
    with pytest.raises(ValueError):
        t["get_lineage"]("reader", None, "gold.g; drop")


def test_an_unconfigured_service_is_a_clear_error():
    t = tools(Fake({}))
    with pytest.raises(LookupError, match="Prometheus is not configured"):
        t["query_metrics"]("reader", None, "files_24h")


def test_the_reader_policy_is_read_only_and_names_real_tools():
    from dip.agents.registry import Policy

    p = Policy.load("reader")
    assert p.max_tier == "read"
    t = tools(Fake({}))
    assert set(t) <= p.tools  # every read tool here is in the reader's policy
    assert json.dumps(sorted(p.tools))  # and the policy has nothing else odd in it
    assert "propose_operation" not in p.tools and "execute_approved_operation" not in p.tools


def test_phi_is_masked_for_a_masking_policy_only_in_what_it_returns():
    from dip.agents.tools import MASK, _masked_detail, _masked_record

    c = load_contracts()["healthcare.eligibility"]
    rec = {"member_id": "M000000001", "first_name": "Alex", "plan_code": "GOLD", "DOB": "1980"}
    masked = _masked_record(rec, c)
    # Declared non-PHI values stay; PHI and unknown columns (PHI under a new name) do not.
    assert masked == {"member_id": "M000000001", "first_name": MASK, "plan_code": "GOLD",
                      "DOB": MASK}  # fmt: skip
    patient = {"resourceType": "Patient", "id": "p1", "birthDate": "1980-01-01",
               "name": [{"family": "Example"}]}  # fmt: skip
    assert _masked_record(patient, c) == {"resourceType": "Patient", "id": "p1",
                                          "fields": ["birthDate", "id", "name", "resourceType"],
                                          "values": MASK}  # fmt: skip
    raw = _masked_record({"header": "a,b,c", "row": "1,Alex,3"}, c)
    assert raw["header"] == "a,b,c" and "Alex" not in raw["row"]
    assert _masked_detail("value '1850-01-01' out of range", "VALUE.OUT_OF_RANGE:dob", c) == MASK
    assert _masked_detail("'GOLD' not allowed", "VALUE.NOT_ALLOWED:plan_code", c) == (
        "'GOLD' not allowed"
    )
    assert _masked_detail("x", "SCHEMA.MISSING_COLUMN:member_id", c) == "x"


def test_the_reader_policy_masks_phi_and_triage_does_not():
    from dip.agents.registry import Policy

    assert Policy.load("reader").mask_phi and not Policy.load("triage").mask_phi
