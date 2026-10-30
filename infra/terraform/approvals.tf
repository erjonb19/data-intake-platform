# The approvals page and audit viewer (Stage 9, S9.2; dip/agents/gateway.py): the MCP
# image, `python -m dip approvals-serve`, ClusterIP only (a port-forward for local use:
# `kubectl -n dip-data port-forward svc/approvals 8081`, then http://127.0.0.1:8081).
#
# Its own warehouse login, dip_approver (infra/terraform/files/postgres-roles.sql, grants
# in ops.grant_approver()): reads ops.approvals and ops.audit_log, appends to the audit
# log, and sets an approval's decision columns. No MinIO, no Airflow, no execution: a
# person's approval is carried out by `dip approvals execute` or the proposing agent's
# Act tier, never by this page. Every /api route needs the bearer token generated here
# (output `approvals_token`), so other pods cannot approve by calling the Service.

resource "random_password" "approvals_token" {
  length  = 40
  special = false
}

resource "kubernetes_secret_v1" "approvals" {
  metadata {
    name      = "dip-approvals"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    DIP_WAREHOUSE_DSN   = "postgresql://dip_approver:${local.pw["pg_approver"]}@${local.postgres_host}:5432/warehouse"
    DIP_APPROVALS_TOKEN = random_password.approvals_token.result
  }
}

resource "kubernetes_deployment_v1" "approvals" {
  metadata {
    name      = "approvals"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
    labels    = { app = "approvals" }
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "approvals" }
    }
    template {
      metadata {
        labels = { app = "approvals" }
        annotations = {
          "dip/config-sha" = sha256(jsonencode(kubernetes_secret_v1.approvals.data))
        }
      }
      spec {
        container {
          name  = "approvals"
          image = "${var.mcp_image_repository}:${var.mcp_image_tag}"
          # The Host names a port-forward or an in-cluster client sends (DNS-rebinding
          # protection refuses any other).
          args = ["python", "-m", "dip", "approvals-serve", "--host", "0.0.0.0", "--port", "8081",
            "--allowed-host", "127.0.0.1", "--allowed-host", "localhost", "--allowed-host", "approvals",
            "--allowed-host", "approvals.${kubernetes_namespace_v1.data.metadata[0].name}.svc",
          "--allowed-host", "approvals.${kubernetes_namespace_v1.data.metadata[0].name}.svc.cluster.local"]
          port {
            name           = "http"
            container_port = 8081
          }
          env_from {
            secret_ref {
              name = kubernetes_secret_v1.approvals.metadata[0].name
            }
          }
          # TCP, like the MCP server's: an HTTP probe's Host header (the pod IP) is not one
          # the page accepts.
          readiness_probe {
            tcp_socket {
              port = 8081
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
            requests = { cpu = "10m", memory = "64Mi" }
            limits   = { memory = "192Mi" }
          }
        }
      }
    }
  }
  wait_for_rollout = true
  depends_on       = [kubernetes_job_v1.postgres_roles]
}

resource "kubernetes_service_v1" "approvals" {
  metadata {
    name      = "approvals"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    selector = { app = "approvals" }
    port {
      name        = "http"
      port        = 8081
      target_port = 8081
    }
  }
}
