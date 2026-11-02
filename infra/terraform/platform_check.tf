# The platform check (Stage 11, S11.6; PLAN.md section 8.13, design choice 1): a CronJob in
# dip-airflow, outside Airflow (a scheduler that is down cannot run a DAG that notices it).
# Every 2 minutes it reads the scheduler's heartbeat from the Airflow API and the pods and
# events of dip-airflow, opens or closes platform incidents in the warehouse, and records
# what it saw (dip/platform_check.py).
#
# Least privilege:
#   Kubernetes  a namespace-scoped Role: get and list pods and events in dip-airflow, and
#               nothing else (no ClusterRole, no kube-state-metrics). The MCP server's own
#               service account is bound to the same Role, for get_k8s_events
#   warehouse   the dip_platform_check login: ops.incident and ops.platform_check only
#   Airflow     the MCP server's Viewer login (the API's health endpoint is public; task
#               instances need a Viewer)

resource "kubernetes_role_v1" "airflow_pod_reader" {
  metadata {
    name      = "dip-airflow-pod-reader"
    namespace = local.airflow_ns
  }
  rule {
    api_groups = [""]
    resources  = ["pods", "events"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_service_account_v1" "platform_check" {
  metadata {
    name      = "platform-check"
    namespace = local.airflow_ns
  }
}

resource "kubernetes_role_binding_v1" "platform_check" {
  metadata {
    name      = "platform-check-pod-reader"
    namespace = local.airflow_ns
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.airflow_pod_reader.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.platform_check.metadata[0].name
    namespace = local.airflow_ns
  }
}

# The MCP server's service account (it had none: the default one, with no Role), bound in
# dip-airflow to the same read-only Role.
resource "kubernetes_service_account_v1" "mcp" {
  metadata {
    name      = "mcp"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
}

resource "kubernetes_role_binding_v1" "mcp" {
  metadata {
    name      = "mcp-pod-reader"
    namespace = local.airflow_ns
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.airflow_pod_reader.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.mcp.metadata[0].name
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
}

resource "kubernetes_secret_v1" "platform_check" {
  metadata {
    name      = "dip-platform-check"
    namespace = local.airflow_ns
  }
  data = {
    DIP_WAREHOUSE_DSN    = "postgresql://dip_platform_check:${local.pw["pg_platform_check"]}@${local.postgres_host}:5432/warehouse"
    DIP_AIRFLOW_URL      = "http://airflow-api-server.${local.airflow_ns}.svc.cluster.local:8080"
    DIP_AIRFLOW_USER     = local.mcp_airflow_user
    DIP_AIRFLOW_PASSWORD = local.pw["airflow_mcp_viewer"]
    DIP_K8S_NAMESPACE    = local.airflow_ns
  }
}

resource "kubernetes_cron_job_v1" "platform_check" {
  metadata {
    name      = "platform-check"
    namespace = local.airflow_ns
  }
  spec {
    schedule                      = "*/2 * * * *"
    concurrency_policy            = "Forbid"
    starting_deadline_seconds     = 60
    successful_jobs_history_limit = 1
    failed_jobs_history_limit     = 2
    job_template {
      metadata {}
      spec {
        backoff_limit           = 0
        active_deadline_seconds = 90
        template {
          metadata {
            labels = { app = "platform-check" }
          }
          spec {
            service_account_name = kubernetes_service_account_v1.platform_check.metadata[0].name
            restart_policy       = "Never"
            container {
              name    = "check"
              image   = "${var.airflow_image_repository}:${var.airflow_image_tag}"
              command = ["python", "-m", "dip.platform_check"]
              env_from {
                secret_ref {
                  name = kubernetes_secret_v1.platform_check.metadata[0].name
                }
              }
              env {
                name  = "DIP_SCHEDULER_STALE_SECONDS"
                value = "90"
              }
              resources {
                requests = { cpu = "50m", memory = "96Mi" }
                limits   = { memory = "256Mi" }
              }
              security_context {
                allow_privilege_escalation = false
              }
            }
          }
        }
      }
    }
  }
  depends_on = [
    kubernetes_job_v1.postgres_roles,
    kubernetes_job_v1.mcp_airflow_viewer,
  ]
}
