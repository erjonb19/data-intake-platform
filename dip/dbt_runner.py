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
    """Run dbt; afterwards, emit OpenLineage events for the models it built (if enabled)."""
    exe = os.environ.get("DIP_DBT_BIN") or shutil.which("dbt") or "dbt"
    cmd = [exe, *args, "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR)]
    result = subprocess.run(cmd, env=dbt_env(dsn), check=False, text=True, encoding="utf-8")
    if args and args[0] in ("build", "run", "seed"):
        from dip import lineage

        target = Path(os.environ.get("DBT_TARGET_PATH") or DBT_DIR / "target")
        if (target / "run_results.json").is_file():
            lineage.dbt_events(lineage.Emitter(), dsn, target)
    if check:
        result.check_returncode()
    return result
