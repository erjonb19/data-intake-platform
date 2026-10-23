# The MCP server, Read tier (Stage 3, S3.4; PLAN.md section 7), in the cluster: Streamable
# HTTP at /mcp, ClusterIP only (a port-forward for local use). One agent is configured
# here, `reader` (agents/policies/reader.yaml): read tools only, every call audited. The
# triage agent's Propose tier stays a local `dip mcp-serve` (Stage 4) until Stage 6.
#
# Least privilege for each thing the server touches:
#   warehouse  the dip_mcp login: reads the ops tables its tools name, inserts into
#              ops.audit_log, nothing in bronze, silver, gold or the research tiers
#   MinIO      a user that may only read raw/ and quarantine/ (samples, file profiles)
#   Airflow    a Viewer login (DAG runs, task logs)
#   Marquez, Prometheus (observability profile)  read-only HTTP APIs
# The reader's key is generated here; only its SHA-256 is in the server's Secret, the key
# itself is the sensitive output `mcp_reader_key`.

locals {
  mcp_minio_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["arn:aws:s3:::raw/*", "arn:aws:s3:::quarantine/*"] },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = ["arn:aws:s3:::raw", "arn:aws:s3:::quarantine"] },
    ]
  })
  mcp_minio_suffix = substr(sha256(local.mcp_minio_policy), 0, 8)
  mcp_minio_user   = "mcp-read-${local.mcp_minio_suffix}"
  mcp_airflow_user = "mcp-viewer"
}

resource "random_password" "mcp_reader_key" {
  length  = 40
  special = false
}

resource "kubernetes_config_map_v1" "mcp_minio_policy" {
  metadata {
    name      = "mcp-minio-policy"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = { "policy.json" = local.mcp_minio_policy }
}

# The Chainguard mc image has no shell: one mc command per container, run in order.
resource "kubernetes_job_v1" "mcp_minio_user" {
  metadata {
    name      = "minio-mcp-${local.mcp_minio_suffix}"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    backoff_limit = 10
    template {
      metadata {}
      spec {
        restart_policy = "OnFailure"
        dynamic "init_container" {
          for_each = {
            "1-policy" = ["admin", "policy", "create", "local", "mcp-read-${local.mcp_minio_suffix}", "/policy/policy.json"]
            "2-user"   = ["admin", "user", "add", "local", local.mcp_minio_user, local.pw["minio_mcp"]]
          }
          content {
            name  = "mc-${init_container.key}"
            image = var.minio_client_image
            args  = init_container.value
            env {
              name  = "MC_HOST_local"
              value = "http://${local.minio_user}:${local.pw["minio_root"]}@${local.minio_host}:9000"
            }
            env {
              name  = "MC_CONFIG_DIR"
              value = "/tmp/mc"
            }
            volume_mount {
              name       = "tmp"
              mount_path = "/tmp"
            }
            volume_mount {
              name       = "policy"
              mount_path = "/policy"
              read_only  = true
            }
          }
        }
        container {
          name  = "mc-3-attach"
          image = var.minio_client_image
          args = ["admin", "policy", "attach", "local", "mcp-read-${local.mcp_minio_suffix}",
          "--user", local.mcp_minio_user]
          env {
            name  = "MC_HOST_local"
            value = "http://${local.minio_user}:${local.pw["minio_root"]}@${local.minio_host}:9000"
          }
          env {
            name  = "MC_CONFIG_DIR"
            value = "/tmp/mc"
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
        }
        volume {
          name = "tmp"
          empty_dir {}
        }
        volume {
          name = "policy"
          config_map {
            name = kubernetes_config_map_v1.mcp_minio_policy.metadata[0].name
          }
        }
      }
    }
  }
  wait_for_completion = true
  timeouts {
    create = "5m"
  }
  depends_on = [kubernetes_job_v1.minio_buckets]
}

# A Viewer login in Airflow for get_dag_run and get_task_log. `users create` prints a
# warning and exits 0 when the user exists, so an existing one is found first and gets its
# password reset to the current one instead (the Job is renamed when the password changes).
resource "kubernetes_job_v1" "mcp_airflow_viewer" {
  metadata {
    name      = "airflow-mcp-viewer-${substr(sha256(local.pw["airflow_mcp_viewer"]), 0, 8)}"
    namespace = local.airflow_ns
  }
  spec {
    backoff_limit = 5
    template {
      metadata {}
      spec {
        restart_policy = "OnFailure"
        container {
          name    = "create-viewer"
          image   = "${var.airflow_image_repository}:${var.airflow_image_tag}"
          command = ["bash", "-c"]
          args = [<<-EOT
            set -euo pipefail
            if airflow users list --output plain | awk '{print $2}' | grep -qx "$VIEWER"; then
              airflow users reset-password --username "$VIEWER" --password "$VIEWER_PASSWORD"
            else
              airflow users create --role Viewer --username "$VIEWER" --password "$VIEWER_PASSWORD" \
                --email mcp-viewer@example.com --firstname MCP --lastname Viewer
            fi
          EOT
          ]
          env {
            name  = "VIEWER"
            value = local.mcp_airflow_user
          }
          env {
            name = "VIEWER_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.mcp_viewer.metadata[0].name
                key  = "password"
              }
            }
          }
          env {
            name = "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"
            value_from {
              secret_key_ref {
                name = "airflow-metadata"
                key  = "connection"
              }
            }
          }
          volume_mount {
            name       = "config"
            mount_path = "/opt/airflow/airflow.cfg"
            sub_path   = "airflow.cfg"
            read_only  = true
          }
        }
        volume {
          name = "config"
          config_map {
            name = "airflow-config"
          }
        }
      }
    }
  }
  wait_for_completion = true
  timeouts {
    create = "5m"
  }
  depends_on = [helm_release.airflow]
}

resource "kubernetes_secret_v1" "mcp_viewer" {
  metadata {
    name      = "mcp-airflow-viewer"
    namespace = local.airflow_ns
  }
  data = { password = local.pw["airflow_mcp_viewer"] }
}

resource "kubernetes_secret_v1" "mcp" {
  metadata {
    name      = "dip-mcp"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    DIP_AGENT_KEYS       = jsonencode({ reader = sha256(random_password.mcp_reader_key.result) })
    DIP_WAREHOUSE_DSN    = "postgresql://dip_mcp:${local.pw["pg_mcp"]}@${local.postgres_host}:5432/warehouse"
    DIP_S3_ENDPOINT      = "http://${local.minio_host}:9000"
    DIP_S3_ACCESS_KEY    = local.mcp_minio_user
    DIP_S3_SECRET_KEY    = local.pw["minio_mcp"]
    DIP_AIRFLOW_URL      = "http://airflow-api-server.${local.airflow_ns}.svc.cluster.local:8080"
    DIP_AIRFLOW_USER     = local.mcp_airflow_user
    DIP_AIRFLOW_PASSWORD = local.pw["airflow_mcp_viewer"]
    DIP_MARQUEZ_URL      = local.observability ? local.marquez_url : ""
    DIP_PROMETHEUS_URL   = local.observability ? "http://prometheus-server.${local.obs_ns}.svc.cluster.local" : ""
  }
}

resource "kubernetes_deployment_v1" "mcp" {
  metadata {
    name      = "mcp"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
    labels    = { app = "mcp" }
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "mcp" }
    }
    template {
      metadata {
        labels = { app = "mcp" }
        annotations = {
          "dip/config-sha" = sha256(jsonencode(kubernetes_secret_v1.mcp.data))
        }
      }
      spec {
        container {
          name  = "mcp"
          image = "${var.mcp_image_repository}:${var.mcp_image_tag}"
          # The Host header a port-forward or an in-cluster client sends (DNS-rebinding
          # protection refuses any other).
          args = ["python", "-m", "dip", "mcp-serve", "--host", "0.0.0.0", "--port", "8765",
            "--allowed-host", "127.0.0.1:*", "--allowed-host", "localhost:*",
          "--allowed-host", "mcp.${kubernetes_namespace_v1.data.metadata[0].name}.svc.cluster.local:*"]
          port {
            name           = "mcp"
            container_port = 8765
          }
          env_from {
            secret_ref {
              name = kubernetes_secret_v1.mcp.metadata[0].name
            }
          }
          readiness_probe {
            tcp_socket {
              port = 8765
            }
            period_seconds = 5
          }
          security_context {
            run_as_non_root            = true
            run_as_user                = 10001
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
          }
          resources {
            requests = { cpu = "20m", memory = "96Mi" }
            limits   = { memory = "256Mi" }
          }
        }
      }
    }
  }
  wait_for_rollout = true
  depends_on = [
    kubernetes_job_v1.mcp_minio_user,
    kubernetes_job_v1.mcp_airflow_viewer,
    kubernetes_job_v1.postgres_roles,
  ]
}

resource "kubernetes_service_v1" "mcp" {
  metadata {
    name      = "mcp"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    selector = { app = "mcp" }
    port {
      name        = "mcp"
      port        = 8765
      target_port = 8765
    }
  }
}
