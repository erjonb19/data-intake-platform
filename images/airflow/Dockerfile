# Platform Airflow image: the official image plus our DAGs, the `dip` package and the
# domain packs (contracts). Build context is the repo root.
FROM apache/airflow:3.2.2-python3.12

# --no-deps: pyyaml, jsonschema and boto3 already ship in the official image at versions
# Airflow's constraints pin, and we must not move them.
COPY --chown=airflow:root pyproject.toml /tmp/dip/pyproject.toml
COPY --chown=airflow:root dip/ /tmp/dip/dip/
RUN pip install --no-cache-dir --no-deps /tmp/dip && rm -rf /tmp/dip

COPY --chown=airflow:root packs/ /opt/airflow/packs/
ENV DIP_CONTRACT_DIRS=/opt/airflow/packs/healthcare/contracts

COPY --chown=airflow:root dags/ /opt/airflow/dags/
