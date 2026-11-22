# Platform Airflow image: the official image plus our DAGs, the `dip` package, the domain
# packs (contracts) and the dbt project. Build context is the repo root.
FROM apache/airflow:3.4.0-python3.12@sha256:818f94affc5bd7ce0be0cc04f2ace09aa8382e65190a73a08881229ff32700cb

# CVE-2026-99306 (CRITICAL, found by the Trivy scan on 2026-11-19): the base image ships
# linux-libc-dev 6.12.115 (kernel headers only; a container runs the host's kernel). Debian's
# fix is 6.12.117-1 in trixie-security, and no rebuilt apache/airflow image has it yet, so
# only that package is upgraded here. Drop this step once the base image carries the fix.
# dip-ocr builds on this image and inherits it.
# The base is a hardened (+dhi0) build that also keeps one record per package in
# /var/lib/dpkg/status.d/, which apt does not update and Trivy reads: after the upgrade
# that record still said 6.12.115 (seen in the build, 2026-11-19). It is rewritten from
# dpkg's own state, so it names what is installed; the build fails if that is below 6.12.117-1.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade linux-libc-dev \
 && rm -rf /var/lib/apt/lists/* \
 && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' linux-libc-dev)" ge 6.12.117-1 \
 && if [ -f /var/lib/dpkg/status.d/linux-libc-dev ]; then \
      dpkg-query -s linux-libc-dev > /var/lib/dpkg/status.d/linux-libc-dev; fi
USER airflow

# 24.4: every pip install reads a hashed lock from requirements/ (scripts/lock.py) with
# --require-hashes --no-deps, so no unpinned or unhashed file is ever downloaded.
COPY --chown=airflow:root requirements/ /tmp/req/
COPY --chown=airflow:root pyproject.toml /tmp/dip/pyproject.toml
COPY --chown=airflow:root dip/ /tmp/dip/dip/
# dip's wheel, built in a throwaway venv with the locked setuptools (--no-build-isolation:
# pip does not fetch a build backend of its own).
RUN python -m venv /tmp/build-venv \
 && /tmp/build-venv/bin/pip install --no-cache-dir --require-hashes --no-deps -r /tmp/req/build.txt \
 && /tmp/build-venv/bin/pip wheel --no-cache-dir --no-deps --no-build-isolation -w /tmp/whl /tmp/dip \
 && rm -rf /tmp/build-venv /tmp/dip
# --no-deps: pyyaml, jsonschema and boto3 already ship in the official image at versions
# Airflow's constraints pin, and we must not move them.
RUN pip install --no-cache-dir --no-deps /tmp/whl/*.whl
# The agents (Stage 9, S9.1: the agent_chain DAG) in their own venv, like dbt below: the
# Anthropic SDK, the MCP client and LangGraph never touch Airflow's dependencies. The
# same pins as pyproject.toml's dev extra and images/mcp (requirements/agent.in).
RUN python -m venv /opt/airflow/agent-venv \
 && /opt/airflow/agent-venv/bin/pip install --no-cache-dir --require-hashes --no-deps -r /tmp/req/build.txt && /opt/airflow/agent-venv/bin/pip install --no-cache-dir --require-hashes --no-deps --no-build-isolation -r /tmp/req/agent.txt \
 && /opt/airflow/agent-venv/bin/pip install --no-cache-dir --no-deps /tmp/whl/*.whl \
 && /opt/airflow/agent-venv/bin/pip check \
 && rm -rf /tmp/whl
COPY --chown=airflow:root agents/prompts/ /opt/airflow/agents/prompts/
COPY --chown=airflow:root config/llm.toml /opt/airflow/config/llm.toml
ENV DIP_PROMPT_DIR=/opt/airflow/agents/prompts     DIP_LLM_CONFIG=/opt/airflow/config/llm.toml
# Excel contracts (Stage 2). Not in the official image; pure Python, one dependency
# (et-xmlfile). Locked against Airflow's constraints (requirements/airflow-extra.in).
RUN pip install --no-cache-dir --require-hashes --no-deps --no-build-isolation -r /tmp/req/airflow-extra.txt

COPY --chown=airflow:root packs/ /opt/airflow/packs/
ENV DIP_CONTRACT_DIRS=/opt/airflow/packs/healthcare/contracts:/opt/airflow/packs/finance/contracts

# dbt in its own venv so its dependencies never touch Airflow's.
RUN python -m venv /opt/airflow/dbt-venv \
 && /opt/airflow/dbt-venv/bin/pip install --no-cache-dir --require-hashes --no-deps -r /tmp/req/build.txt && /opt/airflow/dbt-venv/bin/pip install --no-cache-dir --require-hashes --no-deps --no-build-isolation -r /tmp/req/dbt.txt \
 && /opt/airflow/dbt-venv/bin/pip check
COPY --chown=airflow:root dbt/ /opt/airflow/dbt/
ENV DIP_DBT_DIR=/opt/airflow/dbt \
    DIP_DBT_BIN=/opt/airflow/dbt-venv/bin/dbt \
    DIP_CACHE_DIR=/tmp/dip-cache \
    DBT_LOG_PATH=/tmp/dbt-logs \
    DBT_TARGET_PATH=/tmp/dbt-target

COPY --chown=airflow:root dags/ /opt/airflow/dags/
