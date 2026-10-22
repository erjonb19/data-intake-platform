# Platform Airflow image: the official image plus our DAGs (and, from Stage 1, the
# platform package and dbt project). Build context is the repo root.
FROM apache/airflow:3.2.2-python3.12

COPY --chown=airflow:root dags/ /opt/airflow/dags/
