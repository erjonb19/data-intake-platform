# Python locks (24.4)

Every Python install in CI, the proof jobs and the images reads one of the `.txt` locks here
with `pip install --require-hashes --no-deps`: an exact version and the SHA-256 of every file,
for the whole dependency tree, so nothing unpinned or unhashed is ever downloaded. dip itself
is then installed from the checkout with `--no-deps --no-build-isolation` (its build backend,
setuptools, is in the lock).

| Lock | From | Used by |
| --- | --- | --- |
| `dev.txt` | `pyproject.toml` (the `dev` extra) and `build.in` | the `ci` test job, `evals.yml`, the proof jobs, a laptop |
| `tools.txt` | `tools.in` (ruff, uv) | the `ci` lint job |
| `e2e.txt` | `e2e.in` | the kind E2E driver (`e2e.yml`, `upgrade.yml`, `proof.yml`) |
| `build.txt` | `build.in` (setuptools) | the Airflow image's throwaway venv that builds dip's wheel |
| `agent.txt` | `agent.in` | the Airflow image's agent venv |
| `dbt.txt` | `dbt.in` | the dbt venv in the Airflow and MCP images |
| `intake-api.txt`, `mcp.txt` | their `.in` | `images/intake-api`, `images/mcp` |
| `airflow-extra.txt`, `ocr.txt` | their `.in`, with Airflow's constraints | the Airflow image's own environment, `images/ocr` |

`airflow-constraints-3.4.0-py3.12.txt` is Airflow's published constraints file for the image's
version, so the two locks that install into Airflow's environment never move a package Airflow
ships. Replace it when the Airflow image moves.

## The cooldown

`cutoff.txt` holds a timestamp; nothing uploaded to PyPI after it is chosen (uv's
`--exclude-newer`). `python scripts/lock.py --refresh` moves it to 7 days ago, so a release has
been public for a week before a lock can pick it up.

## Commands

```bash
pip install --require-hashes --no-deps -r requirements/tools.txt   # uv and ruff
python scripts/lock.py --refresh   # new cutoff (7 days ago), compile every lock
python scripts/lock.py             # compile at the current cutoff (after editing an .in or pyproject.toml)
python scripts/lock.py --check     # what CI runs: fail if a lock differs from a fresh compile

# a laptop venv
pip install --require-hashes --no-deps -r requirements/dev.txt
pip install --no-deps --no-build-isolation -e .
```

A pin bump (Dependabot or by hand) changes `pyproject.toml` or an `.in` file and needs the
locks compiled again in the same PR. If the new version is newer than the cutoff, the compile
fails until the cutoff passes it: that is the cooldown at work.
