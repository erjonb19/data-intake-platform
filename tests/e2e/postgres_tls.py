"""24.38 (part 1): Postgres takes TCP logins only over TLS.

From the host, through the NodePort (WAREHOUSE_DSN): a login that refuses TLS
(sslmode=disable) is turned away by pg_hba.conf; libpq's default login gets TLS 1.2 or
newer, as `pg_stat_ssl` shows for its own connection; and with the CA from Terraform's state
(`tls_self_signed_cert.ca`, read here from `terraform output`), verify-full accepts the
server's certificate for `localhost`.

    WAREHOUSE_DSN=... python tests/e2e/postgres_tls.py
"""

from __future__ import annotations

import os
import sys

import psycopg


def main() -> int:
    dsn = os.environ["WAREHOUSE_DSN"]
    failures = []
    try:
        psycopg.connect(dsn, sslmode="disable", connect_timeout=10).close()
        failures.append("a login without TLS was accepted")
    except psycopg.OperationalError as e:
        if "no pg_hba.conf entry" not in str(e) and "SSL off" not in str(e):
            failures.append(f"a login without TLS failed, but not on pg_hba.conf: {e}")
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("select ssl, version from pg_stat_ssl where pid = pg_backend_pid()")
        ssl, version = cur.fetchone()
    if not ssl or version not in ("TLSv1.2", "TLSv1.3"):
        failures.append(f"the default login is not over TLS 1.2+: ssl={ssl} {version}")
    ca = os.environ.get("DIP_CA_FILE")
    if ca:
        try:
            psycopg.connect(dsn, sslmode="verify-full", sslrootcert=ca, connect_timeout=10).close()
        except psycopg.OperationalError as e:
            failures.append(f"verify-full against the cluster's CA failed: {e}")
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if not failures:
        print(f"PASS postgres TLS: plain logins refused, the default login on {version}"
              + (", verify-full against the CA" if ca else ""))  # fmt: skip
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
