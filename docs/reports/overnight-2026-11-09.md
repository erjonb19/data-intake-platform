# Overnight run, 2026-11-08 to 2026-11-09

The run worked through `docs/BACKLOG.md` in its "Order of work", under the standing OK: a PR
is merged once lint, test and both e2e runs are green and a fresh review finds nothing
blocking. No model was called and nothing was spent. No tag was pushed and nothing was
published. Branch protection was not touched, and no other repo was touched.

## Merged

| PR | Item | What it does |
|---|---|---|
| #111 | 12.5 `.wslconfig` guidance (#85) | README "WSL2 memory on Windows"; `scripts/prereqs.sh` prints the `memory=` it finds, or warns |
| #112 | Backlog: order of work, Stages 19 to 23 | Your docs PR from another session: reviewed, green, merged |
| #113 | 12.6 Demos for Stages 4 and 5 (#86, #87) | `docs/demos/stage-4.md` and `stage-5.md`, run on the laptop with a throwaway Postgres and no model key |
| #114 | 12.7 §8.9 refresh (#100) and the screenshot (#99) | PLAN.md §8.9 matches the S11.12 audit; the intake audit screenshot in the README |
| #117 | 12.10 release workflow (the item stays open) | `release.yml` publishes only on a `v*` tag push; notes from PROGRESS; a manual run is a dry run |

## Open, and why

| PR | Item | State |
|---|---|---|
| #115 | 12.8 PDF decompression-bomb check (#97, security) | Five security-review rounds; the last found nothing blocking. Lint, test and one e2e green; the other e2e failed on a Postgres deadlock unrelated to the change (below), and its re-run was still going when this was written |
| #118 | 14.1 Backup CronJob | Review clean. Its e2e passed (the backup runs, the dump's size, hash and `pg_restore --list` check out). The final CI run, after merging `main`, was still going |
| #119 | 14.2 Restore drill | Opened by the 14.2 build, stacked on #118's branch. Not reviewed yet |
| #116 | 12.9 Dependabot, CodeQL, Trivy | **Blocked**; reason under 12.9 in BACKLOG on its branch, and below |

A PR above that goes green after this file was written was merged under the same rule only
if its review was clean; `gh pr list --state merged` is the record.

## Waiting on you

1. **12.9's image scan fails on the Airflow base image.** Trivy finds about 20 CRITICAL
   findings with fixes in `apache/airflow:3.2.2-python3.12` (Airflow itself, fixed in 3.3.0;
   litellm, Pillow, Authlib, PyJWT, GitPython, the SFTP provider, certifi, uv; Debian's brotli
   and perl). `dip-mcp` and `dip-intake-api` pass. Decide: add an Airflow 3.3 upgrade item
   first, or accept the base-image findings in `.trivyignore` with a dated review. Branch
   protection is yours to turn on (the steps are in BACKLOG 12.9).
2. **The first release tag, `v0.12.0`.** `release.yml` is on `main`; nothing is published
   until you push the tag (and 12.9 is done, per the order of work).
3. **Runbooks.** Runbook 07 does not name `PDF.DECOMPRESSION_BOMB`, and Stage 14's runbook
   (`14-restore.md`) was not written: any change under `docs/runbooks/` changes the incident
   agent's recordings and needs a re-recording you approve. The restore steps are in
   `docs/backups.md` instead.
4. **14.3, versioned migrations, was not started.** It changes how `warehouse.sql` is
   applied, and 14.1 and 14.2 both add tables to it, so it comes after they merge. The CI
   deadlock below is real evidence for it.

## Found tonight

- **A Postgres deadlock in the cluster** (#115's e2e, run 42617255988): every intake task runs
  `Warehouse.init`, which re-applies all of `warehouse.sql` with its `ALTER TABLE`s, and its
  locks deadlocked with the MCP server's `propose_operation`. Written under 14.3 in BACKLOG
  (on #118's branch); applying migrations once, before the services, removes it.
- **Peak memory**, now reported by every e2e run's summary: 5.0 GiB of the runner's 15.6 GiB
  with 14.1's backup (run 42617546697), under the 12 GiB budget.
- **Stage 14's incident** (in `docs/incidents.md` on #118's branch): the first backup E2E hung
  on `pg_restore` reading from stdin over `kubectl exec`, and promtool refused two
  `BackupMissing` rules with the same labels.

## How to check each item

**12.5** (merged)
1. `bash scripts/prereqs.sh` on Windows: the "WSL2 (.wslconfig)" section shows `memory=9GB`.
2. `USERPROFILE=/nonexistent bash scripts/prereqs.sh`: it warns, and still exits 0.

**12.6** (merged)
1. Start a throwaway Postgres as in `docs/demos/stage-4.md`, section 3.
2. `DIP_EVAL_DSN=postgresql://dip:dip@127.0.0.1:15999/dip_eval python -m dip triage loop`: 12 PASS.
3. `python -m dip contracts eval` with the same DSN: 13 PASS. Stop the container after.

**12.7** (merged)
1. Read PLAN.md §8.9: Stages 4, 5 and 6 say Partly with their issues; Stages 9 to 11 have rows.
2. The README's intake section shows both screenshots.

**12.8** (#115)
1. `pytest tests/unit/test_pdffeed.py`: 35 passed, 2 skipped without Tesseract.
2. Read `_decode_budget` in `dip/pdffeed.py` and the follow-ups under 12.8 in BACKLOG.

**12.9** (#116, blocked)
1. Open #116's `trivy` check: `dip-airflow` and `dip-ocr` fail, `dip-mcp` and `dip-intake-api` pass.

**12.10** (merged; not released)
1. `gh workflow run release.yml --ref main`: a dry run that builds the four images and writes
   the notes to the run summary, and publishes nothing.
2. `pytest tests/unit/test_release_notes.py`: 5 passed.

**14.1** (#118)
1. After merge, `bash scripts/up.sh`, then `python -m dip backup now`: it prints the
   `ops.backup` row with status `ok`, the object key and its SHA-256.
2. `pytest tests/unit/test_backup.py`; `promtool test rules` runs in the `lint` job.
3. The e2e run's summary shows the peak memory.

**14.2** (#119)
1. Not reviewed tonight: read its PR description, then review it before merging #118 and it.

## Two things to know

- **Two merged branches were deleted.** #111 and #112 were merged with
  `gh pr merge --delete-branch` (`s12-5-wslconfig`, `docs-backlog-scale-access`) before I
  noticed that tonight's rules list deleting branches as a stop. Both were fully merged into
  `main`, so nothing is lost; every merge after that kept its branch.
- **The laptop ran low on memory three times**, and Claude Code stopped a background CI wait
  each time. CI checks after that were single short calls. The throwaway `postgres:17`
  container from the 12.6 demos was stopped.
