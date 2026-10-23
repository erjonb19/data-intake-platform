# Local-only credentials. Generated once per cluster and kept in local Terraform state
# (gitignored). Alphanumeric only so they embed safely in connection URIs.

locals {
  passwords = toset([
    "pg_superuser",
    "pg_airflow",
    "pg_warehouse",
    "pg_marquez",
    "minio_root",
    "airflow_admin",
    "airflow_api_secret",
    "airflow_jwt_secret",
    "dip_deid_key",     # HMAC key for the de-identified research tier (R4)
    "minio_intake_api", # the intake API's MinIO user: put into landing only (S2.3)
    "pg_cohort_reader", # read-only login for the drift monitor's cohort service (R7)
    "pg_metrics",       # read-only login for the warehouse metrics exporter (S3.2)
    "grafana_admin",    # Grafana's admin login (observability profile, S3.2)
  ])
}

resource "random_password" "this" {
  for_each = local.passwords
  length   = 32
  special  = false
}

# Fernet keys must be url-safe base64 of exactly 32 bytes.
resource "random_bytes" "airflow_fernet" {
  length = 32
}

locals {
  pw = { for k, v in random_password.this : k => v.result }

  airflow_fernet_key = replace(replace(random_bytes.airflow_fernet.base64, "+", "-"), "/", "_")

  postgres_host = "postgres.${kubernetes_namespace_v1.data.metadata[0].name}.svc.cluster.local"
  minio_host    = "minio.${kubernetes_namespace_v1.data.metadata[0].name}.svc.cluster.local"
  minio_user    = "dip-admin"
}
