# Intake API (Stage 2, PLAN.md §8.6 S2.3): the `api` channel. One API key per pack,
# generated here; the API's Secret holds only their SHA-256 and the feeds each may send.
# The raw keys are in local Terraform state (gitignored) and the sensitive output
# `intake_api_keys`, never in the repo. ClusterIP only: reach it with a port-forward. Its
# MinIO user may only list and write the landing bucket.

locals {
  api_clients = { for a in local.sftp_accounts : a => "${a}.*" }
  # The API never needs more than listing and writing landing: not raw, not quarantine.
  api_minio_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["arn:aws:s3:::landing/*"] },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = ["arn:aws:s3:::landing"] },
    ]
  })
  # Versioned names: a changed policy gets a fresh user and policy (mc refuses to attach a
  # policy that is already attached, so re-running the same names would fail).
  api_minio_suffix = substr(sha256(local.api_minio_policy), 0, 8)
  api_minio_user   = "intake-api-${local.api_minio_suffix}"
}

resource "kubernetes_config_map_v1" "intake_api_minio_policy" {
  metadata {
    name      = "intake-api-minio-policy"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = { "policy.json" = local.api_minio_policy }
}

# The Chainguard mc image has no shell: one mc command per container, run in order.
resource "kubernetes_job_v1" "intake_api_minio_user" {
  metadata {
    name      = "minio-intake-api-${local.api_minio_suffix}"
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
            "1-policy" = ["admin", "policy", "create", "local", "intake-api-landing-${local.api_minio_suffix}", "/policy/policy.json"]
            "2-user"   = ["admin", "user", "add", "local", local.api_minio_user, local.pw["minio_intake_api"]]
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
          args = ["admin", "policy", "attach", "local", "intake-api-landing-${local.api_minio_suffix}",
          "--user", local.api_minio_user]
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
            name = kubernetes_config_map_v1.intake_api_minio_policy.metadata[0].name
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
    DIP_MINIO_USER     = local.api_minio_user
    DIP_MINIO_PASSWORD = local.pw["minio_intake_api"]
    DIP_API_MAX_BYTES  = tostring(20 * 1024 * 1024)
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
          "dip/config-sha" = sha256(jsonencode(kubernetes_secret_v1.intake_api.data))
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
            # A 20 MB upload is held twice while it is checked and sent on.
            requests = { cpu = "20m", memory = "64Mi" }
            limits   = { memory = "256Mi" }
          }
        }
      }
    }
  }
  wait_for_rollout = true
  depends_on       = [kubernetes_job_v1.intake_api_minio_user]
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
