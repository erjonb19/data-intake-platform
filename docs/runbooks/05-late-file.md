# Runbook 5: a late file

**Signal.** An open `FEED.LATE` incident in `ops.incident`, opened by the `feed_sla` DAG
when no file arrived within the contract's `expected_arrival` window (cron plus grace).
Alert **FeedLate**; the Feeds dashboard's "Open late-file incidents" stat and "Freshness"
panel.

**Break it on purpose:** do not send the file. With the eligibility contract's
`expected_arrival: { cron: "0 6 * * *", grace: 2h }`, a day with no file opens an incident
at the next `feed_sla` run after 08:00. `python -m dip break late_file` writes the good
file unchanged (only its timing is broken); the break loop holds it back, checks that
`FEED.LATE` opens with nothing quarantined, then delivers it an hour late and checks that
the next SLA check closes the incident.

## Evidence

1. `query_metrics(freshness_seconds, feed)`: time since the newest file.
2. `get_feed_history(feed)`: the last file and its date.
3. `get_dag_run("feed_sla")` and its task log: the check that opened the incident.
4. `get_dag_run("sftp_sync")`: for an SFTP feed, did the sync run, and did it find files
   it refused (a feed not allowed on that channel, a `.filepart` still being written)?

## Decide

| What you see | Action |
|---|---|
| Nothing arrived anywhere | Contact the vendor (no platform action: the incident closes itself) |
| The file sits in the vendor's SFTP folder under the wrong name, or in another feed's folder | Ask the vendor to rename or resend; the sync takes only matching names |
| It arrived but was quarantined | Not late: follow that reason code's runbook |

## Check

The incident closes on its own: an arrival after its `since` time closes it at the next
`feed_sla` run (`ops.incident.closed_at`), and **FeedLate** clears.
