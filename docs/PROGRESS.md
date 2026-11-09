# Progress

One line per finished item from [BACKLOG.md](BACKLOG.md): what changed, and how to check it.
Newest last.

Stages 0 to 11 were finished before this file existed. Their record is PLAN.md §8 (the
decisions log), `docs/demos/stage-N.md` and `docs/reports/` (the Stage 10 and 11 summaries
and the S11.12 audit).

## Model spend

Metered by `cost_usd` (the meter the caps use; it errs high: October was $1.84 on the
Anthropic Console against about $3.51 metered). Cap: $5 per calendar month on the paid key.

| As of | All time (metered) | This month (metered) | Last run that spent |
|---|---|---|---|
| 2026-11-05 | about $4.07 ($3.51 to 2026-10-31, plus $0.561 for Stage 11) | at most $0.561 (Stage 11 started 2026-10-31, so part of it was October) | S11.11 live eval, run 41618947945, $0.0354 |

## Finished items

| Date | Item | What changed | How to check it |
|---|---|---|---|
| 2026-11-06 | 12.1 Nightly evals green (#83) | The nightly `evals` check compares the evals the job started (`reports/expected.txt`) with the reports they wrote, instead of expecting exactly 6 (`scripts/check_eval_reports.py`, 8 unit tests). Incident write-up in `docs/incidents.md`. No model spend | `gh workflow run evals.yml --ref main` once merged; the last step prints `all 7 evals wrote a report and passed every case` (proof on the branch's final commit: run 42140268435) |
| 2026-11-06 | 12.2 E2E flakes (#101, #103) | Break loop: the resend is named `elig_YYYYMMDD_resend.csv` with the quarantined file's business day, so a loop crossing midnight UTC still closes the group. Platform drill: the check Job is made from the CronJob's template without its owner reference (3-minute TTL), so the CronJob's history limit cannot delete it before it is read. 5 unit tests. No model spend | `pytest tests/unit/test_break_loop_resend_name.py tests/unit/test_platform_drill_check_job.py`; both kind `e2e` runs (push and pull request) on the PR's final commit |
| 2026-11-06 | 12.3 README honest, Production readiness checklist | The README says it runs on Kubernetes locally (kind on a laptop, the same on GitHub runners; no cloud host). The checklist has a line per feature in five groups: built ones name their file, CI job or drill; unbuilt ones say "not yet built" and name their backlog item. `tests/unit/test_readme_readiness.py` fails on a named path that does not exist or a backlog item that is not in BACKLOG. CLAUDE.md gets the memory budget from Stage 13 on. No model spend | `pytest tests/unit/test_readme_readiness.py`; read the README's "Production readiness" section |
| 2026-11-07 | 12.4 License: Apache-2.0 (#84) | `LICENSE` is the Apache License 2.0 text, unchanged. `NOTICE` names the copyright holder and the committed Airflow chart with its own license. `pyproject.toml` declares `license = "Apache-2.0"` (SPDX, setuptools 77 or later), so the wheel carries `License-Expression: Apache-2.0` and both files; the images, which copy only `pyproject.toml` and `dip/`, still build. README has a License section. 3 unit tests. No model spend | `pytest tests/unit/test_license.py`; `pip wheel --no-deps . -w /tmp/w` and look for `License-Expression: Apache-2.0` in the wheel's METADATA |
| 2026-11-07 | 12.5 `.wslconfig` guidance (#85) | README section "WSL2 memory on Windows": why the WSL2 cap is the memory Docker and kind get, the `.wslconfig` for a 16 GB laptop (`memory=9GB`, `swap=4GB`, measured), starting points for 8 GB and 32 GB (marked untested), and `wsl --shutdown` to apply it. `scripts/prereqs.sh` prints the `memory=` it finds on Windows, or warns if there is none; it never fails on it. No model spend | `bash scripts/prereqs.sh` on Windows: the "WSL2 (.wslconfig)" section shows `memory=...`; with `USERPROFILE=/nonexistent` it warns |
| 2026-11-08 | 12.8 PDF decompression-bomb check (#97) | `dip/pdffeed.py` counts what every Flate stream would inflate to before pdfminer parses the file, in bounded chunks and without keeping the output, and refuses past 256 MB with `PDF.DECOMPRESSION_BOMB`. It inflates from each stream's start until zlib says it ended (compressed bytes can contain `endstream`), accepts any zlib window size, decodes ASCII85 and ASCIIHex wrappers (reportlab's default) and follows a Flate layer inside another. LZW, run-length and encrypted streams are not counted (documented). 8 new unit tests. Runbook 07 is model input, so it does not list the new code yet. No model spend | `pytest tests/unit/test_pdffeed.py` (24 passed, 2 skipped without Tesseract) |
