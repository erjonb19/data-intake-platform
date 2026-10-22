# Platform Airflow image: the official image plus our DAGs, the `dip` package, the domain
# packs (contracts) and the dbt project. Build context is the repo root.
FROM apache/airflow:3.2.2-python3.12

# --no-deps: pyyaml, jsonschema and boto3 already ship in the official image at versions
# Airflow's constraints pin, and we must not move them.
COPY --chown=airflow:root pyproject.toml /tmp/dip/pyproject.toml
COPY --chown=airflow:root dip/ /tmp/dip/dip/
RUN pip install --no-cache-dir --no-deps /tmp/dip && rm -rf /tmp/dip
# Excel contracts (Stage 2). Not in the official image; pure Python, one dependency (et-xmlfile).
RUN pip install --no-cache-dir "openpyxl==3.1.5" "et-xmlfile==2.0.0"

COPY --chown=airflow:root packs/ /opt/airflow/packs/
ENV DIP_CONTRACT_DIRS=/opt/airflow/packs/healthcare/contracts

# dbt in its own venv so its dependencies never touch Airflow's.
RUN python -m venv /opt/airflow/dbt-venv \
 && /opt/airflow/dbt-venv/bin/pip install --no-cache-dir "dbt-core==1.12.6" "dbt-postgres==1.11.0"
COPY --chown=airflow:root dbt/ /opt/airflow/dbt/
ENV DIP_DBT_DIR=/opt/airflow/dbt \
    DIP_DBT_BIN=/opt/airflow/dbt-venv/bin/dbt \
    DIP_CACHE_DIR=/tmp/dip-cache \
    DBT_LOG_PATH=/tmp/dbt-logs \
    DBT_TARGET_PATH=/tmp/dbt-target

COPY --chown=airflow:root dags/ /opt/airflow/dags/
