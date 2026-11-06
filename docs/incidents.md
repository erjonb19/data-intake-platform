# Incidents

Real breaks on this platform, one per stage from Stage 12: what broke, how we knew, how we
fixed it, and what stops it coming back. Drills (`dip drill`, `dip break`) have their own
runbooks in `docs/runbooks/`; this file is for the ones nobody planned. It is not in that
folder on purpose: every `.md` there (except its README) is in the incident agent's runbook
search, and its hits are part of every incident recording's fingerprint.

## 2026-11-03: the nightly evals went red with every case passing (Stage 12, #83)

**What broke.** The nightly `evals` workflow failed three nights running (runs
41631832492, 41827356431, 42027130487). Every one of the seven evals replayed and passed
every case. The last step, "Every case passed", then failed with
`7 reports (expected 6); failed: []`. S11.5 (#73) had added a seventh eval,
`triage-from-rejections`, to the replay step, and nobody changed the hard-coded 6 in the
check below it. The check was right to guard against a missing report; it guarded with a
number that had to be kept in step by hand.

**How we knew.** The S11.12 audit found it (gap 1 in `docs/reports/audit.md`) on
2026-11-03, the day of the first red run. Nothing alerted: a failed scheduled workflow
shows only in the Actions tab and in GitHub's failure email. The run log's last
lines gave the cause directly.

**How we fixed it.** 12.1 (the PR for #83). The replay step's `run()` writes each eval's
name to `reports/expected.txt` as it starts it, and the check is now
`scripts/check_eval_reports.py`: it fails when a started eval wrote no report, when a report
is there that no eval started, when a report has zero cases or cannot be read, or when any
case failed. Adding an eval now needs no other line changed. Unit tests cover each failure,
and one test reads `evals.yml` to check that every eval goes through `run()` and that the
check is the script.

**Check.** Dispatch `evals` on `main` (Actions → evals → Run workflow, or
`gh workflow run evals.yml`): the last step prints
`all 7 evals wrote a report and passed every case`.

**A second break while fixing it.** The first version of this write-up went into
`docs/runbooks/incidents.md`. The proof run on the branch (42139290188) then failed the
`incident` eval: `schema_drift_hands_off_incident.json, turn 4: the agent sent the model
something other than what was recorded`. The new file had changed what `search_runbooks`
returns. Moving it here fixed the replay without re-recording.

**What stops it coming back.** The list of evals is now in one place. CLAUDE.md now says that any new or changed file
in `docs/runbooks/` means re-recording the incident cases. The missing piece is an
alert when an eval drops: that is Stage 16.3 (Alertmanager with a receiver, and an alert on
the nightly eval result).
