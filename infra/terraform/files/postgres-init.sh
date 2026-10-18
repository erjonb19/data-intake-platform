#!/bin/sh
# Runs once, on first initialization of an empty data volume.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE airflow   LOGIN PASSWORD '${AIRFLOW_DB_PASSWORD}';
CREATE ROLE warehouse LOGIN PASSWORD '${WAREHOUSE_DB_PASSWORD}';
CREATE ROLE marquez   LOGIN PASSWORD '${MARQUEZ_DB_PASSWORD}';

CREATE DATABASE airflow   OWNER airflow;
CREATE DATABASE warehouse OWNER warehouse;
CREATE DATABASE marquez   OWNER marquez;
SQL

# Medallion and operations schemas live in the warehouse database.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname warehouse <<SQL
CREATE SCHEMA IF NOT EXISTS bronze AUTHORIZATION warehouse;
CREATE SCHEMA IF NOT EXISTS silver AUTHORIZATION warehouse;
CREATE SCHEMA IF NOT EXISTS gold   AUTHORIZATION warehouse;
CREATE SCHEMA IF NOT EXISTS ops    AUTHORIZATION warehouse;
SQL
