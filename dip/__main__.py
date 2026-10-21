"""Command line: `python -m dip <command>`.

  db-init                      create the bronze and ops tables
  land <feed> <files...>       copy local files into the landing bucket
  intake                       process everything in the landing bucket
  vocab-load                   download (pinned, checksummed) and load the OMOP vocabulary
  deid-key-install             store DIP_DEID_KEY for the de-identified tier
  dbt <args...>                run dbt against the warehouse, e.g. `dbt build`

Storage is a local directory (`--store-dir`, default ./data/store) unless S3 settings are
given in the environment (DIP_S3_ENDPOINT, DIP_S3_ACCESS_KEY, DIP_S3_SECRET_KEY).
The warehouse is DIP_WAREHOUSE_DSN.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from dip import BUCKET_LANDING
from dip.contracts import load_contracts
from dip.intake import process_landing
from dip.storage import LocalStore, ObjectStore, S3Store
from dip.warehouse import Warehouse


def _store(args: argparse.Namespace) -> ObjectStore:
    if endpoint := os.environ.get("DIP_S3_ENDPOINT"):
        import boto3

        return S3Store(
            boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=os.environ["DIP_S3_ACCESS_KEY"],
                aws_secret_access_key=os.environ["DIP_S3_SECRET_KEY"],
                region_name="us-east-1",
            )
        )
    return LocalStore(args.store_dir)


def _warehouse() -> Warehouse:
    import psycopg

    return Warehouse(psycopg.connect(os.environ["DIP_WAREHOUSE_DSN"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dip")
    parser.add_argument("--store-dir", default="data/store")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("db-init")
    land = sub.add_parser("land")
    land.add_argument("feed")
    land.add_argument("files", nargs="+", type=Path)
    sub.add_parser("intake")
    vocab = sub.add_parser("vocab-load")
    vocab.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    sub.add_parser("deid-key-install")
    dbt = sub.add_parser("dbt")
    dbt.add_argument("dbt_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if args.command == "db-init":
        _warehouse().init()
    elif args.command == "land":
        store = _store(args)
        for f in args.files:
            store.put(BUCKET_LANDING, f"{args.feed}/{f.name}", f.read_bytes())
        print(f"landed {len(args.files)} file(s) for {args.feed}")
    elif args.command == "intake":
        outcomes = process_landing(_store(args), _warehouse(), load_contracts())
        for o in outcomes:
            print(json.dumps(asdict(o)))
        statuses = [o.status for o in outcomes]
        print(
            f"{len(outcomes)} file(s): "
            + ", ".join(f"{s}={statuses.count(s)}" for s in sorted(set(statuses))),
            file=sys.stderr,
        )
    elif args.command == "vocab-load":
        from dip import vocab

        concepts, maps_to = vocab.load(_warehouse().conn, vocab.fetch(args.cache_dir))
        print(f"loaded {concepts} concepts and {maps_to} 'Maps to' relationships into vocab")
    elif args.command == "deid-key-install":
        from dip.deid import install_key

        fp = install_key(_warehouse().conn, os.environ.get("DIP_DEID_KEY"))
        print(f"de-identification key installed (fingerprint {fp})")
    elif args.command == "dbt":
        from dip.dbt_runner import run_dbt

        return run_dbt(os.environ["DIP_WAREHOUSE_DSN"], *args.dbt_args, check=False).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
