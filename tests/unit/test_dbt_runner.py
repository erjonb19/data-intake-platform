from __future__ import annotations

from types import SimpleNamespace

from dip.dbt_runner import connection_dsn, dbt_env


def test_connection_dsn_survives_special_characters():
    conn = SimpleNamespace(login="ware house", password="p/ss@w:rd%", host="pg", port=None,
                           schema="warehouse")  # fmt: skip
    dsn = connection_dsn(conn)
    assert dsn == "postgresql://ware%20house:p%2Fss%40w%3Ard%25@pg:5432/warehouse"
    env = dbt_env(dsn)
    assert (env["DIP_PG_USER"], env["DIP_PG_PASSWORD"]) == ("ware house", "p/ss@w:rd%")
    assert (env["DIP_PG_HOST"], env["DIP_PG_PORT"], env["DIP_PG_DB"]) == ("pg", "5432", "warehouse")
