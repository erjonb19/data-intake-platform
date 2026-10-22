# Intake API (Stage 2, PLAN.md §8.6 S2.3): the `api` channel. One API key per pack,
# generated here; the API's Secret holds only their SHA-256 and the feeds each may send.
# The raw keys are in local Terraform state (gitignored) and the sensitive output
# `intake_api_keys`, never in the repo. ClusterIP only: reach it with a port-forward.

locals {
  api_clients = { for a in local.sftp_accounts : a => "${a}.*" }
}

resource "random_password" "api_key" {
  for_each = local.api_clients
  length   = 40
  special  = false
}

resource "kubernetes_secret_v1" "intake_api" {
  metadata {
    name      = "intake-api"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    DIP_API_CLIENTS = jsonencode({
      for name, feeds in local.api_clients : name => {
        sha256 = sha256(random_password.api_key[name].result)
        feeds  = [feeds]
      }
    })
    DIP_MINIO_URL      = "http://${local.minio_host}:9000"
    DIP_MINIO_USER     = local.minio_user
    DIP_MINIO_PASSWORD = local.pw["minio_root"]
  }
}

resource "kubernetes_deployment_v1" "intake_api" {
  metadata {
    name      = "intake-api"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
    labels    = { app = "intake-api" }
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "intake-api" }
    }
    template {
      metadata {
        labels = { app = "intake-api" }
        annotations = {
          # Roll the pod when the client list changes (env from a Secret is read at start).
          "dip/clients-sha" = sha256(kubernetes_secret_v1.intake_api.data["DIP_API_CLIENTS"])
        }
      }
      spec {
        container {
          name  = "api"
          image = "${var.intake_api_image_repository}:${var.intake_api_image_tag}"
          port {
            name           = "http"
            container_port = 8000
          }
          env_from {
            secret_ref {
              name = kubernetes_secret_v1.intake_api.metadata[0].name
            }
          }
          readiness_probe {
            http_get {
              path = "/healthz"
              port = 8000
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
            requests = { cpu = "20m", memory = "64Mi" }
            limits   = { memory = "192Mi" }
          }
        }
      }
    }
  }
  wait_for_rollout = true
  depends_on       = [kubernetes_job_v1.minio_buckets]
}

resource "kubernetes_service_v1" "intake_api" {
  metadata {
    name      = "intake-api"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    selector = { app = "intake-api" }
    port {
      name        = "http"
      port        = 8000
      target_port = 8000
    }
  }
}
