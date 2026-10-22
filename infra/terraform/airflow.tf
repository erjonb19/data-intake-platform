locals {
  airflow_ns = kubernetes_namespace_v1.airflow.metadata[0].name

  airflow_secrets = {
    "airflow-metadata"       = { connection = "postgresql://airflow:${local.pw["pg_airflow"]}@${local.postgres_host}:5432/airflow" }
    "airflow-fernet-key"     = { "fernet-key" = local.airflow_fernet_key }
    "airflow-api-secret-key" = { "api-secret-key" = local.pw["airflow_api_secret"] }
    "airflow-jwt-secret"     = { "jwt-secret" = local.pw["airflow_jwt_secret"] }
    "dip-deid"               = { key = local.pw["dip_deid_key"] }
    # SFTP channel: where the server is, its pinned host key, and the platform's login key.
    "dip-sftp" = {
      host     = local.sftp_host
      host_key = tls_private_key.sftp_host.public_key_openssh
      key      = tls_private_key.sftp_platform.private_key_openssh
      accounts = join(",", local.sftp_accounts)
    }

    # Airflow connections, injected as AIRFLOW_CONN_* env vars into every Airflow pod
    # (including KubernetesExecutor task pods) via the chart's `secret` list.
    "airflow-connections" = {
      minio = jsonencode({
        conn_type = "aws"
        login     = local.minio_user
        password  = local.pw["minio_root"]
        extra = {
          endpoint_url = "http://${local.minio_host}:9000"
          region_name  = "us-east-1"
        }
      })
      warehouse = "postgresql://warehouse:${local.pw["pg_warehouse"]}@${local.postgres_host}:5432/warehouse"
    }
  }
}

resource "kubernetes_secret_v1" "airflow" {
  for_each = local.airflow_secrets

  metadata {
    name      = each.key
    namespace = local.airflow_ns
  }
  data = each.value
}

resource "helm_release" "airflow" {
  name       = "airflow"
  repository = "https://airflow.apache.org"
  chart      = "airflow"
  version    = var.airflow_chart_version
  namespace  = local.airflow_ns

  values = [file("${path.module}/../helm-values/airflow.yaml")]

  set = [
    { name = "images.airflow.repository", value = var.airflow_image_repository },
    { name = "images.airflow.tag", value = var.airflow_image_tag },
  ]

  set_sensitive = [
    { name = "createUserJob.defaultUser.password", value = local.pw["airflow_admin"] },
  ]

  # Migrations and user creation run as plain Jobs (useHelmHooks=false in the values
  # file) so that `wait` does not deadlock on post-install hooks.
  wait          = true
  wait_for_jobs = true
  timeout       = 900

  depends_on = [
    kubernetes_secret_v1.airflow,
    kubernetes_stateful_set_v1.postgres,
    kubernetes_service_v1.postgres,
    kubernetes_job_v1.minio_buckets,
  ]
}
