"""Command line: `python -m dip <command>`.

  db-init                      create the bronze and ops tables
  land <feed> <files...>       copy local files into the landing bucket
  intake                       process everything in the landing bucket
  vocab-load                   load the OMOP vocabulary: the pinned Eunomia shard, or
                               local Athena folders (--athena / DIP_ATHENA_DIRS)
  vocab-coverage               unmapped rows and codes per OMOP table (counts only)
  deid-key-install             store DIP_DEID_KEY for the de-identified tier
  dbt <args...>                run dbt against the warehouse, e.g. `dbt build`
  extract build --version V    write a versioned, checksummed extract of omop_shareable
  extract verify --version V   rebuild (dbt build) and diff byte for byte against version V

Storage is a local directory (`--store-dir`, default ./data/store) unless S3 settings are
given in the environment (DIP_S3_ENDPOINT, DIP_S3_ACCESS_KEY, DIP_S3_SECRET_KEY).
The warehouse is DIP_WAREHOUSE_DSN.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from dip import BUCKET_LANDING
from dip.contracts import load_contracts
from dip.intake import feed_of, process_landing
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


def _extract(args: argparse.Namespace) -> int:
    from dip import extract
    from dip.dbt_runner import run_dbt

    conn = _warehouse().conn
    if args.action == "build":
        from dip import lineage

        r = extract.build(conn, args.out, args.version, args.license)
        lineage.extract_event(
            lineage.Emitter(), os.environ["DIP_WAREHOUSE_DSN"], r, list(extract.TABLES)
        )
        state = "reproduced the existing" if r.reproduced_existing else "wrote"
        print(f"{state} extract {args.version} at {r.path} (manifest sha256 {r.manifest_sha256})")
        return 0
    published = args.out / args.version
    if not (published / extract.MANIFEST).is_file():
        print(f"no published extract at {published}", file=sys.stderr)
        return 2
    run_dbt(os.environ["DIP_WAREHOUSE_DSN"], "build")
    rebuilt = args.out / f".verify-{args.version}"
    shutil.rmtree(rebuilt, ignore_errors=True)
    for name, data in extract.build_files(conn, args.version, args.license).items():
        (rebuilt / name).parent.mkdir(parents=True, exist_ok=True)
        (rebuilt / name).write_bytes(data)
    differences = extract.diff(published, rebuilt)
    shutil.rmtree(rebuilt, ignore_errors=True)
    if differences:
        print(f"NOT REPRODUCIBLE: {len(differences)} file(s) differ: {differences}")
        return 1
    print(f"reproducible: rebuild of {args.version} is byte-identical")
    return 0


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
    # Full local Athena download(s) instead of the Eunomia shard; default DIP_ATHENA_DIRS.
    vocab.add_argument("--athena", type=Path, nargs="+", default=None)
    sub.add_parser("vocab-coverage")
    sub.add_parser("deid-key-install")
    ext = sub.add_parser("extract")
    ext.add_argument("action", choices=["build", "verify"])
    ext.add_argument("--version", required=True)
    ext.add_argument("--out", type=Path, default=Path("data/extracts"))
    # Default: CC0-1.0 for synthetic sources; ODbL-1.0 (required) for MIMIC-derived ones.
    ext.add_argument("--license", default=None)
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
        from dip import lineage

        outcomes = process_landing(_store(args), _warehouse(), load_contracts())
        emitter = lineage.Emitter()
        for o in outcomes:
            lineage.intake_event(emitter, os.environ["DIP_WAREHOUSE_DSN"], feed_of(o.source_key), o)
            print(json.dumps(asdict(o)))
        statuses = [o.status for o in outcomes]
        print(
            f"{len(outcomes)} file(s): "
            + ", ".join(f"{s}={statuses.count(s)}" for s in sorted(set(statuses))),
            file=sys.stderr,
        )
    elif args.command == "vocab-load":
        from dip import vocab

        conn = _warehouse().conn
        dirs = args.athena or vocab.athena_dirs()
        if dirs:
            concepts, maps_to = vocab.load_athena(conn, dirs)
        else:
            concepts, maps_to = vocab.load(conn, vocab.fetch(args.cache_dir))
        print(f"loaded {concepts} concepts and {maps_to} 'Maps to' relationships into vocab")
        print(json.dumps(vocab.provenance(conn)))
        for vocabulary_id, g in vocab.mapping_gaps(conn).items():
            print(
                f"warning: {g['without_maps_to']:,} of {g['non_standard']:,} non-standard"
                f" {vocabulary_id} concepts have no 'Maps to'. Athena exports a mapping"
                " only when both vocabularies are in the same download.",
                file=sys.stderr,
            )
    elif args.command == "vocab-coverage":
        from dip import coverage, vocab

        conn = _warehouse().conn
        print(json.dumps({"vocabulary": vocab.provenance(conn), **coverage.unmapped(conn)}))
    elif args.command == "deid-key-install":
        from dip.deid import install_key

        fp = install_key(_warehouse().conn, os.environ.get("DIP_DEID_KEY"))
        print(f"de-identification key installed (fingerprint {fp})")
    elif args.command == "extract":
        return _extract(args)
    elif args.command == "dbt":
        from dip.dbt_runner import run_dbt

        return run_dbt(os.environ["DIP_WAREHOUSE_DSN"], *args.dbt_args, check=False).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
