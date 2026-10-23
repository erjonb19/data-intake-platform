"""Writes to the warehouse (Postgres): bronze rows and ops bookkeeping.

Takes any DB-API connection with `%s` parameters, so it works with psycopg 3 locally and
with the psycopg2 connection that Airflow's PostgresHook returns.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from dip.fhir import Resource

if TYPE_CHECKING:
    from dip.csvfeed import Row


@dataclass(frozen=True)
class IntakeRecord:
    file_sha256: str
    feed: str
    contract_version: int
    source_key: str
    raw_key: str
    status: str
    accepted_count: int
    rejected_count: int
    skipped_count: int
    received_at: datetime
    file_date: date | None = None  # the validated business date in the file name
    channel: str = "minio"  # how the file arrived: minio, sftp or api (Stage 2)


@dataclass(frozen=True)
class QuarantineRecord:
    feed: str
    file_sha256: str
    level: str
    reason_code: str
    detail: str
    record_ref: str | None
    quarantine_key: str


def init_sql() -> str:
    return files("dip").joinpath("sql/warehouse.sql").read_text("utf-8")


class Warehouse:
    def __init__(self, conn: Any):
        self.conn = conn

    def init(self) -> None:
        from dip.vocab import STCM_DDL

        with self.conn.cursor() as cur:
            cur.execute(init_sql())
            # Every dbt build reads it (source_to_standard). Created here too, not only by a
            # vocabulary load: a warehouse whose vocabulary predates the table never reloads.
            cur.execute(STCM_DDL)
        self.conn.commit()

    def already_processed(self, feed: str, file_sha256: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "select 1 from ops.intake_file where feed = %s and file_sha256 = %s",
                (feed, file_sha256),
            )
            return cur.fetchone() is not None

    def record_outcome(
        self,
        intake: IntakeRecord,
        quarantine: list[QuarantineRecord],
        resources: list[Resource],
        patient_id: str | None,
        loaded_at: datetime,
        csv_rows: Sequence[Row] = (),
    ) -> int:
        """Bronze rows, quarantine rows and the intake row commit together or not at all.
        Returns how many open quarantine entries the file superseded (_close_resent)."""
        try:
            with self.conn.cursor() as cur:
                if csv_rows:
                    cur.executemany(
                        "insert into bronze.csv_record (feed, file_sha256, row_number, record,"
                        " loaded_at) values (%s, %s, %s, %s::jsonb, %s)",
                        [
                            (
                                intake.feed,
                                intake.file_sha256,
                                r.row_number,
                                json.dumps(r.record, sort_keys=True),
                                loaded_at,
                            )
                            for r in csv_rows
                        ],
                    )
                if resources:
                    cur.executemany(
                        "insert into bronze.fhir_resource (feed, file_sha256, entry_index,"
                        " resource_type, resource_id, patient_id, resource, loaded_at)"
                        " values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                        [
                            (
                                intake.feed,
                                intake.file_sha256,
                                r.entry_index,
                                r.resource_type,
                                r.resource_id,
                                patient_id,
                                json.dumps(r.body, sort_keys=True),
                                loaded_at,
                            )
                            for r in resources
                        ],
                    )
                _insert_quarantine(cur, quarantine)
                cur.execute(
                    "insert into ops.intake_file (file_sha256, feed, contract_version, source_key,"
                    " raw_key, status, accepted_count, rejected_count, skipped_count, received_at)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        intake.file_sha256,
                        intake.feed,
                        intake.contract_version,
                        intake.source_key,
                        intake.raw_key,
                        intake.status,
                        intake.accepted_count,
                        intake.rejected_count,
                        intake.skipped_count,
                        intake.received_at,
                    ),
                )
                _insert_delivery(cur, intake)
                superseded = _close_resent(cur, intake) if intake.status == "loaded" else 0
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return superseded

    def record_delivery(self, intake: IntakeRecord) -> bool:
        """Record another delivery of content already processed; False if already known."""
        try:
            with self.conn.cursor() as cur:
                inserted = _insert_delivery(cur, intake)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return inserted

    def record_quarantine(self, quarantine: list[QuarantineRecord]) -> None:
        try:
            with self.conn.cursor() as cur:
                _insert_quarantine(cur, quarantine)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise


def _insert_delivery(cur: Any, intake: IntakeRecord) -> bool:
    cur.execute(
        "insert into ops.intake_delivery"
        " (feed, file_sha256, source_key, file_date, received_at, channel)"
        " values (%s, %s, %s, %s, %s, %s) on conflict do nothing",
        (
            intake.feed,
            intake.file_sha256,
            intake.source_key,
            intake.file_date,
            intake.received_at,
            intake.channel,
        ),
    )
    return cur.rowcount == 1


def _insert_quarantine(cur: Any, quarantine: list[QuarantineRecord]) -> None:
    if not quarantine:
        return
    cur.executemany(
        "insert into ops.quarantine (feed, file_sha256, level, reason_code,"
        " detail, record_ref, quarantine_key) values (%s, %s, %s, %s, %s, %s, %s)",
        [
            (
                q.feed,
                q.file_sha256,
                q.level,
                q.reason_code,
                q.detail,
                q.record_ref,
                q.quarantine_key,
            )
            for q in quarantine
        ],
    )


def _close_resent(cur: Any, intake: IntakeRecord) -> int:
    """The vendor's resend closes the open quarantine a person's executed request_resend
    covered (status `superseded`, pointing at the resend). A resend is a file of the same
    feed, loaded whole, with the same business date as the file it replaces (the date in
    its name, else the UTC day it was received): the next routine delivery of an
    incremental feed (another day's claims) never closes yesterday's quarantine. Entries no
    executed resend request covers stay open, and so do entries of files quarantined for
    their name (they have no delivery, so no business date). Returns how many closed."""
    day = intake.file_date or intake.received_at.astimezone(UTC).date()
    cur.execute(
        "update ops.quarantine q set status = 'superseded', superseded_by = %s"
        " from ops.intake_delivery d"
        " where q.status = 'open' and q.feed = %s and q.file_sha256 <> %s"
        "   and d.feed = q.feed and d.file_sha256 = q.file_sha256"
        "   and coalesce(d.file_date, (d.received_at at time zone 'UTC')::date) = %s"
        "   and q.quarantine_id in ("
        "     select (jsonb_array_elements_text(payload -> 'quarantine_ids'))::bigint"
        "     from ops.approvals where action = 'request_resend' and status = 'executed'"
        "       and payload ->> 'feed' = %s)",
        (intake.file_sha256, intake.feed, intake.file_sha256, day, intake.feed),
    )
    return cur.rowcount


class DryRunWarehouse:
    """Records outcomes in memory and writes nothing: for "what would happen" reports."""

    def __init__(self):
        self.intake: list[IntakeRecord] = []
        self.deliveries: set[tuple[str, str, str]] = set()
        self.quarantine: list[QuarantineRecord] = []
        self.loaded: int = 0

    def already_processed(self, feed: str, file_sha256: str) -> bool:
        return any(i.feed == feed and i.file_sha256 == file_sha256 for i in self.intake)

    def record_outcome(
        self, intake, quarantine, resources, patient_id, loaded_at, csv_rows=()
    ) -> int:
        self.intake.append(intake)
        self.quarantine.extend(quarantine)
        self.loaded += len(resources) + len(csv_rows)
        self.deliveries.add((intake.feed, intake.file_sha256, intake.source_key))
        return 0

    def record_delivery(self, intake) -> bool:
        key = (intake.feed, intake.file_sha256, intake.source_key)
        if key in self.deliveries:
            return False
        self.deliveries.add(key)
        return True

    def record_quarantine(self, quarantine) -> None:
        self.quarantine.extend(quarantine)
