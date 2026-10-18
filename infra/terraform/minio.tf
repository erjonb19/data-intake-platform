resource "kubernetes_secret_v1" "minio" {
  metadata {
    name      = "minio-credentials"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    MINIO_ROOT_USER     = local.minio_user
    MINIO_ROOT_PASSWORD = local.pw["minio_root"]
  }
}

resource "kubernetes_persistent_volume_claim_v1" "minio" {
  metadata {
    name      = "minio-data"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    access_modes = ["ReadWriteOnce"]
    resources {
      requests = { storage = "5Gi" }
    }
  }
  # kind's local-path provisioner binds on first consumer.
  wait_until_bound = false
}

resource "kubernetes_deployment_v1" "minio" {
  metadata {
    name      = "minio"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
    labels    = { app = "minio" }
  }

  spec {
    replicas = 1
    strategy {
      type = "Recreate" # RWO volume
    }

    selector {
      match_labels = { app = "minio" }
    }

    template {
      metadata {
        labels = { app = "minio" }
      }

      spec {
        container {
          name  = "minio"
          image = var.minio_image
          args  = ["server", "/data", "--address", ":9000", "--console-address", ":9001"]

          env_from {
            secret_ref {
              name = kubernetes_secret_v1.minio.metadata[0].name
            }
          }

          port {
            name           = "api"
            container_port = 9000
          }
          port {
            name           = "console"
            container_port = 9001
          }

          volume_mount {
            name       = "data"
            mount_path = "/data"
          }

          readiness_probe {
            http_get {
              path = "/minio/health/ready"
              port = 9000
            }
            period_seconds = 5
          }
          liveness_probe {
            http_get {
              path = "/minio/health/live"
              port = 9000
            }
            initial_delay_seconds = 10
            period_seconds        = 20
          }

          resources {
            requests = { cpu = "50m", memory = "128Mi" }
            limits   = { memory = "512Mi" }
          }
        }

        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.minio.metadata[0].name
          }
        }
      }
    }
  }

  wait_for_rollout = true

  timeouts {
    create = "5m"
    update = "5m"
  }
}

resource "kubernetes_service_v1" "minio" {
  metadata {
    name      = "minio"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    selector = { app = "minio" }
    port {
      name        = "api"
      port        = 9000
      target_port = 9000
    }
    port {
      name        = "console"
      port        = 9001
      target_port = 9001
    }
  }
}

resource "kubernetes_service_v1" "minio_nodeport" {
  metadata {
    name      = "minio-nodeport"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    type     = "NodePort"
    selector = { app = "minio" }
    port {
      name        = "api"
      port        = 9000
      target_port = 9000
      node_port   = local.node_ports.minio_api
    }
    port {
      name        = "console"
      port        = 9001
      target_port = 9001
      node_port   = local.node_ports.minio_console
    }
  }
}

# Idempotent bucket creation. The Chainguard image has no shell, so the alias is
# supplied through MC_HOST_<alias> and a single `mc mb` call creates every bucket.
resource "kubernetes_job_v1" "minio_buckets" {
  metadata {
    name      = "minio-buckets-${substr(sha256(join(",", var.minio_buckets)), 0, 8)}"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }

  spec {
    backoff_limit = 10

    template {
      metadata {}
      spec {
        restart_policy = "OnFailure"

        container {
          name  = "mc"
          image = var.minio_client_image
          args  = concat(["mb", "--ignore-existing"], [for b in var.minio_buckets : "local/${b}"])

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
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "5m"
  }

  depends_on = [kubernetes_deployment_v1.minio, kubernetes_service_v1.minio]
}
