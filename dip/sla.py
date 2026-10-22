"""Late files (Stage 2, S2.6): `FEED.LATE` incidents from each contract's expected_arrival.

A contract says when its file is due: `expected_arrival: {cron: "0 6 * * *", grace: 2h}`
(a 5-field cron in UTC, and how long after it the file may still arrive). At time `now`,
the window being judged is the latest scheduled time `due` with `due + grace <= now`. The
feed is **late** for that window if nothing arrived since the scheduled time before it
(`previous`): no delivery (loaded, quarantined, resent) and no file quarantined for its name.
A bad file is not a late file.

`check()` opens one incident per feed and missed window (ops.incident, unique per feed, kind
and window) and closes open ones whose feed has had an arrival since. The `feed_sla` DAG
runs it every 15 minutes; Prometheus alerting on it arrives with Stage 3.

The cron reader is small on purpose: minute hour day-of-month month day-of-week, each `*`,
a number, a range `a-b`, a list `a,b`, or a step `*/n` or `a-b/n`; day-of-week 0-6 with
0 = Sunday (7 is also Sunday). Like cron, when both day fields are restricted a day matches
either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from dip.contracts import Contract

DDL = """
CREATE TABLE IF NOT EXISTS ops.incident (
    incident_id   bigserial   PRIMARY KEY,
    feed          text        NOT NULL,
    kind          text        NOT NULL,
    window_start  timestamptz NOT NULL,
    since         timestamptz NOT NULL,   -- an arrival after this closes the incident
    opened_at     timestamptz NOT NULL DEFAULT now(),
    closed_at     timestamptz,
    detail        text,
    UNIQUE (feed, kind, window_start)
);
"""
FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
LOOKBACK = timedelta(days=35)


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        if not (lo <= a <= b <= hi) or step < 1:
            raise ValueError(f"cron field {spec!r} is outside {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, expr: str) -> Cron:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron {expr!r}: expected 5 fields")
        m, h, d, mo, w = (_field(p, *FIELDS[i]) for i, p in enumerate(parts))
        w = {0 if x == 7 else x for x in w}
        return cls(frozenset(m), frozenset(h), frozenset(d), frozenset(mo), frozenset(w),
                   parts[2] == "*", parts[4] == "*")  # fmt: skip

    def _day_ok(self, t: datetime) -> bool:
        dom = t.day in self.days
        dow = (t.isoweekday() % 7) in self.weekdays
        if self.any_day or self.any_weekday:
            return dom and dow
        return dom or dow  # cron: either restricted day field matches

    def previous(self, before: datetime) -> datetime | None:
        """The latest scheduled time at or before `before` (minute precision, UTC)."""
        t = before.astimezone(UTC).replace(second=0, microsecond=0)
        stop = t - LOOKBACK
        while t > stop:
            if t.month in self.months and self._day_ok(t) and t.hour in self.hours:
                if t.minute in self.minutes:
                    return t
                t -= timedelta(minutes=1)
            else:
                t = t.replace(minute=0) - timedelta(minutes=1)  # skip the rest of the hour
        return None


def parse_grace(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", str(text))
    if not m:
        raise ValueError(f"grace {text!r}: use minutes, hours or days, e.g. 90m, 2h, 1d")
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


@dataclass(frozen=True)
class Window:
    feed: str
    due: datetime
    previous: datetime
    deadline: datetime


def window(contract: Contract, now: datetime) -> Window | None:
    rule = contract.spec.get("expected_arrival")
    if not rule:
        return None
    cron, grace = Cron.parse(rule["cron"]), parse_grace(rule["grace"])
    due = cron.previous(now - grace)
    if due is None:
        return None
    previous = cron.previous(due - timedelta(minutes=1)) or due - LOOKBACK
    return Window(contract.feed, due, previous, due + grace)


def _arrived(cur: Any, feed: str, since: datetime) -> bool:
    cur.execute(
        "select exists (select 1 from ops.intake_delivery where feed = %s and received_at > %s)"
        " or exists (select 1 from ops.quarantine where feed = %s and created_at > %s)",
        (feed, since, feed, since),
    )
    return bool(cur.fetchone()[0])


def check(conn: Any, contracts: dict[str, Contract], now: datetime) -> dict[str, list[str]]:
    """Open and close FEED.LATE incidents; returns what changed."""
    opened, closed = [], []
    with conn.cursor() as cur:
        cur.execute(DDL)
        for feed, contract in sorted(contracts.items()):
            w = window(contract, now)
            if w is not None and not _arrived(cur, feed, w.previous):
                cur.execute(
                    "insert into ops.incident (feed, kind, window_start, since, detail)"
                    " values (%s, 'FEED.LATE', %s, %s, %s) on conflict do nothing"
                    " returning incident_id",
                    (feed, w.due, w.previous,
                     f"nothing arrived after {w.previous:%Y-%m-%d %H:%M} UTC;"
                     f" due {w.due:%Y-%m-%d %H:%M}, deadline {w.deadline:%Y-%m-%d %H:%M}"),
                )  # fmt: skip
                if cur.fetchone():
                    opened.append(f"{feed} {w.due:%Y-%m-%d %H:%M}")
            cur.execute(
                "select incident_id, window_start, since from ops.incident where feed = %s"
                " and kind = 'FEED.LATE' and closed_at is null order by window_start",
                (feed,),
            )
            for incident_id, window_start, since in cur.fetchall():
                if _arrived(cur, feed, since):  # the late file (or a later one) came
                    cur.execute("update ops.incident set closed_at = %s where incident_id = %s",
                                (now, incident_id))  # fmt: skip
                    closed.append(f"{feed} {window_start:%Y-%m-%d %H:%M}")
    conn.commit()
    return {"opened": opened, "closed": closed}
