# Overnight report, 2026-11-11 (to 2026-11-12)

The owner's instructions of 2026-11-11, worked unattended, then the one-at-a-time merge queue
the owner set the next morning. No live model calls, no recordings, no release tags, no
deploys, no data deleted. Permission settings changed only on the owner's explicit OK (#132).
Model spend: **$0**.

## Merged (21 PRs)

Each one with CI green on its final commit and a review by a separate agent that did not write
it; every blocking finding was fixed and re-checked before the merge.

| PR | What |
| --- | --- |
| #126 | 24.1 security audit (plan only), as approved |
| #127 | The owner's rules: the merge rule (CLAUDE.md, PLAN log), Stage 24's Order of work, the 12.10 note, 24.7, 24.43 |
| #128 | Dependabot: paramiko on 3.5.x, anthropic as its own PR, Python 3.12 base images |
| #124, #120 | Dependabot actions update; the overnight report of 2026-11-09 |
| #138 | **Pillow 12.3.0 to 13.0.0** (Dependabot security update): the repo's 41 open alerts, 5 critical, were all this pin in the dev extra |
| #129 | **24.2 (High)** Airflow pods stop holding MinIO root, the de-identification key and the SFTP key |
| #130 | **24.3 (High)** a hard monthly spend cap on the scheduled agents, failing closed |
| #131 | **24.4 (High)** locked, hashed Python installs everywhere with a 7-day cooldown (uv, approved) |
| #133 | 14.3 versioned warehouse migrations; with it a 14.2 fix to the restore drill (below) |
| #134 | 24.5 every GitHub Action pinned by commit SHA |
| #135 | 24.8 a kill switch for the agents' write tools |
| #136 | 24.10 chain run ids carry a signed token the MCP server checks |
| #137 | 24.11 one approvals token per person; a decision is the token owner's |
| #142 | 24.12 backups leave out the de-identification key's rows |
| #143 | 24.13 no password in any Job spec |
| #144 | 24.22 the platform's images pinned by digest |
| #145 | 24.36 the SFTP sync refuses unsafe names and oversized files |
| #132 | 24.42 `.claude/settings.json`, with the owner's extra denies (tags, pushes to `main`, tag pushes) |
| #139 | Dependabot: Python base image 3.12.14 to 3.12.16 |
| #147 | 24.23 Dependabot's own 7-day cooldown, pins raised but never widened, paramiko ignored by version |

## Closed

- **#123** (Python 3.15 base images): dbt-core 1.12.6 cannot install on 3.15.
- **#141** (grouped Python updates): it widened paramiko to `<5.2` again, five of its bumps
  were inside the lock's 7-day cooldown, and the locks were not recompiled. #147 fixed the
  Dependabot config so the next one can merge.
- **#125** (the old grouped Python PR): not merged, as asked; after `@dependabot recreate`,
  Dependabot closed it itself as superseded.

## Open

- **#146** Dependabot **security** update, paramiko 3.5.1 to 5.0.0 for a low-severity alert
  (RSA keys and SHA-1). **Needs the owner:** it conflicts with "paramiko stays on 3.5.x", and
  the fix exists only in 5.0.0. The platform uses Ed25519 keys only (`dags/sftp_sync.py`), so
  the RSA path is not used.

## Found and fixed on the way

- **The restore drill could report a restore that did not happen.** The `upgrade` E2E's second
  drill failed three times: `kubectl exec -i` sometimes lost the short stdin carrying the
  `pg_restore -L` list, an empty list restores nothing and exits 0, and the next query found no
  table. Fixed in #133, with a test; written up in `docs/incidents.md` (2026-11-11). This is
  Stage 14's incident.
- **Migrations and late logins.** 14.3 stopped re-applying the schema on every task, which also
  stopped re-applying grants to logins created later. The deploy Job now calls every
  `ops.grant_*()` after `migrate`, under the schema's advisory lock.
- **An sdist built with an unhashed build tool.** `dbt-core-experimental-parser` ships as source
  only; with build isolation pip fetched `packaging` unpinned. Every locked install now runs
  `--no-build-isolation` after a hashed build lock (#131).
- **Run tokens could be skipped with a bare chain id** (`intake-y` without a step), and the
  kind E2E's own call lacked a token. Both fixed before #136 merged.
- **The approvals token `moved` block** broke `up.sh`'s targeted apply; dropped, so an upgrade
  generates a new first-approver token (read it again with `terraform output`).

## Not done

| Item | Why |
| --- | --- |
| 24.6 branch protection | Owner's setting. It is on for `main` (required checks; force push and deletion blocked; no required reviews; admins not enforced); left open for the owner to say whether that is all 24.6 asks |
| 24.9 agent runners' own login, 24.18 security alerts | Built locally on 14.3, 24.3 and 24.8, which are now merged; not yet opened (the queue came first) |
| 24.24 SECURITY.md, 24.25, 24.26, 24.34, 24.40 (report mode) | Built locally, waiting their turn |
| 24.14 append-only audit log | Needs the `ops` schema owned by a separate role, a cross-cutting ownership change; not started |
| 24.15 pod security, 24.16 network policies | Not started |
| 24.17 PHI masked for triage, 24.29 exfiltration evals | Change what an agent sees, so they need new recordings (**ask**) |
| 24.19 pip-audit, 24.20 gitleaks, 24.21 zizmor | New tools (**ask**) |
| 24.35 generic errors to the model | Changes tool results, so recordings (**ask**) |
| Runbook `14-restore.md` | `docs/runbooks/` is model input: re-recording (**ask**) |
| 24.43 the three CodeQL alerts | Not started (two look like false positives) |

## Notes for the owner

- **CI throughput was the limit.** Each push starts the `ci` and `e2e` workflows twice (on
  `push` and on `pull_request`), and every merge made the next PR conflict on
  `docs/PROGRESS.md`'s last lines. The one-at-a-time queue fixed most of it. Restricting
  `push:` to `main` would halve CI use, but CLAUDE.md says "CI on every push", so it is the
  owner's call.
- **`.claude/settings.json` review notes** (nothing blocking): some forms are not denied (a
  push to `x:main` or `HEAD:refs/heads/main`, `--follow-tags`, `--mirror`; the PowerShell side
  lacks `gh release`, `gh repo delete`, `git branch -D`), and the five `WebFetch(domain:...)`
  allows never apply because the bare `WebFetch` ask wins. Owner's call; nothing added.
- **12.10** stays open until the owner links the GHCR packages and re-runs the release.
- **Dependabot pip PRs** now need `python scripts/lock.py` added by a person
  (`requirements/README.md`).

## How to check

- `gh pr list --state merged --limit 25` lists the merged PRs above.
- `docs/PROGRESS.md` has one line per item, with its own check; `docs/BACKLOG.md` Stage 24 is
  ticked to match.
- `python scripts/lock.py --check` (after `pip install --require-hashes --no-deps -r
  requirements/tools.txt`) shows every lock is current.
