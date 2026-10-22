"""Run the dbt project against a warehouse DSN (CLI, tests and the Airflow DAG share this)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

DBT_DIR = Path(os.environ.get("DIP_DBT_DIR", Path(__file__).resolve().parent.parent / "dbt"))


def dbt_env(dsn: str) -> dict[str, str]:
    u = urlsplit(dsn)
    return {
        **os.environ,
        "DIP_PG_HOST": u.hostname or "localhost",
        "DIP_PG_PORT": str(u.port or 5432),
        "DIP_PG_USER": unquote(u.username or ""),
        "DIP_PG_PASSWORD": unquote(u.password or ""),
        "DIP_PG_DB": u.path.lstrip("/"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "PYTHONIOENCODING": "utf-8",
    }


def run_dbt(dsn: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    exe = os.environ.get("DIP_DBT_BIN") or shutil.which("dbt") or "dbt"
    cmd = [exe, *args, "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR)]
    return subprocess.run(cmd, env=dbt_env(dsn), check=check, text=True, encoding="utf-8")
