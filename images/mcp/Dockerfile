# The MCP server (Stage 3, S3.4: the Read tier, deployed): the dip package and the official
# MCP SDK, served by uvicorn (python -m dip mcp-serve). Build context is the repo root. Base
# and packages pinned; every version was at least two weeks old when it was chosen. No
# Anthropic SDK: the server never calls a model, agents do. The same image serves the
# approvals page (Stage 9, S9.2: python -m dip approvals-serve, FastAPI, its own login).
FROM python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# 24.4: hashed locks (requirements/mcp.in and dbt.in, compiled by scripts/lock.py),
# installed with --require-hashes --no-deps. mcp.txt includes setuptools, dip's build backend.
COPY requirements/build.txt requirements/mcp.txt requirements/dbt.txt /tmp/req/
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/req/build.txt && pip install --no-cache-dir --require-hashes --no-deps --no-build-isolation -r /tmp/req/mcp.txt \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp

# Stage 10 (S10.6): dbt for check_dbt_models (`dbt parse` of the models generated from a
# candidate contract; no database connection), in its own venv as in the Airflow image so
# its dependencies never touch the server's. Same lock as the Airflow image.
RUN python -m venv /opt/dbt-venv \
 && /opt/dbt-venv/bin/pip install --no-cache-dir --require-hashes --no-deps -r /tmp/req/build.txt && /opt/dbt-venv/bin/pip install --no-cache-dir --require-hashes --no-deps --no-build-isolation -r /tmp/req/dbt.txt \
 && /opt/dbt-venv/bin/pip check \
 && rm -rf /tmp/req

COPY pyproject.toml /tmp/dip/pyproject.toml
COPY dip/ /tmp/dip/dip/
RUN pip install --no-cache-dir --no-deps --no-build-isolation /tmp/dip && rm -rf /tmp/dip
COPY packs/ /app/packs/
COPY agents/policies/ /app/agents/policies/
COPY docs/runbooks/ /app/docs/runbooks/
COPY dbt/ /app/dbt/
ENV DIP_CONTRACT_DIRS=/app/packs/healthcare/contracts:/app/packs/finance/contracts \
    DIP_POLICY_DIR=/app/agents/policies \
    DIP_RUNBOOK_DIR=/app/docs/runbooks \
    DIP_REPO_ROOT=/app \
    DIP_DBT_BIN=/opt/dbt-venv/bin/dbt \
    HOME=/tmp

USER 10001
EXPOSE 8765
CMD ["python", "-m", "dip", "mcp-serve", "--host", "0.0.0.0", "--port", "8765"]
