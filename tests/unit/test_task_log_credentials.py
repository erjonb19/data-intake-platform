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
    # 24.27: secret-named keys, Authorization headers, tokens known by their prefix
    ("Authorization: Bearer abc.def-123 sent", "Authorization: Bearer *** sent"),
    ('{"authorization": "Basic dXNlcjpwdw=="}', '{"authorization": "Basic ***"}'),
    ("x-api-key: zzz9", "x-api-key: ***"),
    ("api_key=AAA client_secret: bbb", "api_key=*** client_secret: ***"),
    ("accessToken: tt clientSecret=ss", "accessToken: *** clientSecret=***"),
    ("x-auth-token: q", "x-auth-token: ***"),
    # the platform's own keys (24.27 review)
    ("DIP_TRIAGE_KEY=abc DIP_DEID_KEY=k", "DIP_TRIAGE_KEY=*** DIP_DEID_KEY=***"),
    ("DIP_AGENT_KEYS=xyz DIP_APPROVER_TOKENS=q", "DIP_AGENT_KEYS=*** DIP_APPROVER_TOKENS=***"),
    ('access_token=xyz refresh_token: "q w"', "access_token=*** refresh_token: ***"),
    ("DIP_RUN_TOKEN_KEY=abc", "DIP_RUN_TOKEN_KEY=***"),
    ("AIRFLOW__CORE__FERNET_KEY=zz", "AIRFLOW__CORE__FERNET_KEY=***"),
    ("aws_secret_access_key=wJalr", "aws_secret_access_key=***"),
    # Made-up values in real shapes; gitleaks:allow keeps the secret scan (24.20) quiet on them.
    ("key sk-ant-api03-ABCDEFGHIJKLMNOPQRST in env", "key *** in env"),  # gitleaks:allow
    ("GEMINI_API_KEY=AIzaSyA1234567890abcdefghijklmnopqrstu", "GEMINI_API_KEY=***"),  # noqa: E501 gitleaks:allow
    ("using AIzaSyA1234567890abcdefghijklmnopqrstu now", "using *** now"),  # gitleaks:allow
    ("id AKIAABCDEFGHIJKLMNOP", "id ***"),  # gitleaks:allow
    ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "***"),  # gitleaks:allow
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc", "jwt ***"),  # gitleaks:allow
    # not credentials: token counts, object keys, the refused-login line (S11.8 shows it)
    ("input_tokens=12 token_count: 3 tokens: 5", "input_tokens=12 token_count: 3 tokens: 5"),
    ("object_key=landing/a.csv", "object_key=landing/a.csv"),
    # prose: a bare secret or token names what failed, and the diagnosis needs it
    ("Mounting secret: dip-agent-chain", "Mounting secret: dip-agent-chain"),
    ("refresh token: 401 Unauthorized", "refresh token: 401 Unauthorized"),
    ('password authentication failed for user "warehouse"',
     'password authentication failed for user "warehouse"'),
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


def test_an_airflow_3_exception_message_reaches_the_log_lines():
    """Airflow 3 puts an exception's message in a structured "exception" field, not in the
    event: the refused login of S11.8's drill is only there."""
    from dip.agents.read_tools import _log_lines

    got = {"content": [
        {"event": "Task failed with exception", "level": "error", "exception": [{
            "exc_type": "OperationalError", "frames": [{"filename": "x.py", "lineno": 1}],
            "exc_value": 'connection failed: FATAL:  password authentication failed for user'
                         ' "warehouse"'}]},
        {"event": "Marking task as FAILED."},
    ]}  # fmt: skip
    assert _log_lines(got) == [
        "Task failed with exception",
        'OperationalError: connection failed: FATAL:  password authentication failed for user'
        ' "warehouse"',
        "Marking task as FAILED.",
    ]  # fmt: skip


@pytest.mark.parametrize("odd", [5, True, None, "Traceback (most recent call last): ...",
                                 {"exc_type": "X"}, [None, 3, {"exc_type": None}]])  # fmt: skip
def test_an_odd_exception_field_never_breaks_the_log(odd):
    from dip.agents.read_tools import _log_lines

    assert _log_lines({"content": [{"event": "e", "exception": odd}]}) == ["e"]


def test_psycopgs_refused_login_survives_phi_masking_and_a_quoted_row_does_not():
    """Review of S11.8: the refused login, as psycopg words it, is a platform line; an
    exception that only quotes one (with a row's values) is not."""
    from dip.agents.read_tools import _log_lines

    got = {"content": [{"event": "Task failed with exception", "exception": [{
        "exc_type": "psycopg.OperationalError",
        "exc_value": 'connection failed: connection to server at "postgres.dip-data"'
                     ' (10.96.0.12), port 5432 failed: FATAL:  password authentication'
                     ' failed for user "warehouse"\n'}]},
        {"event": "Marking task as FAILED. dag_id=x, task_id=intake"},
        {"event": "e", "exception": [{"exc_type": "ValueError",
                                       "exc_value": "bad dob 1980-02-14 for Alex Hallett;"
                                                    " Marking task as FAILED"}]}]}  # fmt: skip
    kept = [ln for ln in _log_lines(got) if _PLATFORM_LOG.search(ln)]
    assert len(kept) == 2 and "password authentication failed" in kept[0]
    assert not any("Hallett" in ln for ln in kept)


def test_the_stored_logs_error_detail_is_read_as_well():
    """Airflow 3.2.2's supervisor stores the exception as "error_detail" (S11.8's E2E:
    the refused login was only there)."""
    from dip.agents.read_tools import _log_lines

    got = {
        "content": [
            {
                "event": "Task failed with exception",
                "error_detail": [
                    {
                        "exc_type": "OperationalError",
                        "exc_value": 'FATAL:  password authentication failed for user "warehouse"',
                    }
                ],
            }
        ]
    }
    assert _log_lines(got)[1] == (
        'OperationalError: FATAL:  password authentication failed for user "warehouse"'
    )


def test_a_non_list_error_detail_does_not_hide_the_exception_list():
    from dip.agents.read_tools import _log_lines

    got = {"content": [{"event": "e", "error_detail": "x",
                        "exception": [{"exc_type": "E", "exc_value": "v"}]}]}  # fmt: skip
    assert _log_lines(got) == ["e", "E: v"]


def test_a_long_line_is_masked_in_linear_time():
    """24.27 review: an unbounded URI scheme made "x-x-x-..." quadratic (5 s at 50 KB)."""
    import time

    for line in ("x-" * 100_000, "a." * 50_000 + "://", "pass_" * 20_000 + "=1"):
        started = time.monotonic()
        _mask_credentials(line)
        assert time.monotonic() - started < 1.0, line[:20]
