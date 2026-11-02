"""Stage 11 (S11.8): get_task_log masks a credential in any log line before the agent sees
it, and shows a masked agent the "password authentication failed" line (a role name)."""

from __future__ import annotations

import pytest

from dip.agents.read_tools import _PLATFORM_LOG, _mask_credentials


@pytest.mark.parametrize(("line", "want"), [
    ("dsn postgresql://warehouse:drill-9f@postgres.dip-data:5432/warehouse",
     "dsn postgresql://warehouse:***@postgres.dip-data:5432/warehouse"),
    ("password=s3cr3t host=postgres", "password=*** host=postgres"),
    ('PASSWORD: "two words" then', "PASSWORD: *** then"),
    ('{"password": "hunter2", "login": "minio"}', '{"password": ***, "login": "minio"}'),
    ("PGPASSWORD=hunter2 psql", "PGPASSWORD=*** psql"),
    ("postgresql://warehouse@postgres/warehouse", "postgresql://warehouse@postgres/warehouse"),
    # not credentials: a URL with a port and a later "@", a working directory
    ("GET http://airflow-api-server:8080/api/v2/x?owner=a@b.com",
     "GET http://airflow-api-server:8080/api/v2/x?owner=a@b.com"),
    ("pwd: /opt/airflow", "pwd: /opt/airflow"),
    ("loaded healthcare.eligibility/a.csv accepted=2 rejected=0 []",
     "loaded healthcare.eligibility/a.csv accepted=2 rejected=0 []"),
])  # fmt: skip
def test_credentials_are_masked(line, want):
    assert _mask_credentials(line) == want


def test_a_refused_login_is_a_platform_line():
    line = ('psycopg.OperationalError: connection failed: connection to server at'
            ' "postgres.dip-data" (10.96.0.12), port 5432 failed: FATAL:  password'
            ' authentication failed for user "warehouse"')  # fmt: skip
    assert _PLATFORM_LOG.search(line)
    # the whole line, as psycopg words it: nothing before or after it gets through
    assert not _PLATFORM_LOG.search('Alex Hallett 1980-02-14 password authentication failed'
                                    ' for user "warehouse"')  # fmt: skip
    assert not _PLATFORM_LOG.search(line + " and Alex Hallett 1980-02-14")
