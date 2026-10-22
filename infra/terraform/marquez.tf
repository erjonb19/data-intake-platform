# Marquez (Stage 3, S3.3), in the observability profile: the OpenLineage API and its web
# UI, on the shared Postgres's `marquez` database (created by postgres-init.sh). The
# platform's own emitter (dip/lineage.py) sends the intake, dbt and extract events to it
# through OPENLINEAGE_URL, so lineage runs from a landed file to gold and on to the
# shareable extract (R6). Search (OpenSearch) is off: it would double the memory.
# ClusterIP only: `kubectl -n dip-obs port-forward svc/marquez-web 3001:3000`.

locals {
  marquez_url = "http://marquez.${local.obs_ns}.svc.cluster.local:5000"
}

resource "kubernetes_config_map_v1" "marquez" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "marquez"
    namespace = kubernetes_namespace_v1.obs[0].metadata[0].name
  }
  data = {
    # The image's own config is for development (OpenSearch on, password "marquez").
    "marquez.yml" = <<-EOT
      server:
        applicationConnectors:
          - type: http
            port: 5000
            httpCompliance: RFC7230_LEGACY
        adminConnectors:
          - type: http
            port: 5001
      db:
        driverClass: org.postgresql.Driver
        url: jdbc:postgresql://${local.postgres_host}:5432/marquez
        user: marquez
        password: $${POSTGRES_PASSWORD}
      migrateOnStartup: true
      graphql:
        enabled: true
      search:
        enabled: false
      logging:
        level: INFO
        appenders:
          - type: console
    EOT
  }
}

resource "kubernetes_secret_v1" "marquez" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "marquez"
    namespace = kubernetes_namespace_v1.obs[0].metadata[0].name
  }
  data = {
    POSTGRES_PASSWORD = local.pw["pg_marquez"]
  }
}

resource "kubernetes_deployment_v1" "marquez" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "marquez"
    namespace = kubernetes_namespace_v1.obs[0].metadata[0].name
    labels    = { app = "marquez" }
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "marquez" }
    }
    template {
      metadata {
        labels = { app = "marquez" }
        annotations = {
          "dip/config-sha" = sha256(jsonencode(kubernetes_config_map_v1.marquez[0].data))
        }
      }
      spec {
        container {
          name  = "api"
          image = var.marquez_image
          env {
            name  = "MARQUEZ_CONFIG"
            value = "/config/marquez.yml"
          }
          env {
            name  = "JAVA_OPTS"
            value = "-Xms128m -Xmx320m"
          }
          env_from {
            secret_ref {
              name = kubernetes_secret_v1.marquez[0].metadata[0].name
            }
          }
          port {
            name           = "api"
            container_port = 5000
          }
          readiness_probe {
            http_get {
              path = "/healthcheck"
              port = 5001
            }
            initial_delay_seconds = 20
            period_seconds        = 10
            failure_threshold     = 30
          }
          volume_mount {
            name       = "config"
            mount_path = "/config"
            read_only  = true
          }
          security_context {
            allow_privilege_escalation = false
          }
          resources {
            requests = { cpu = "50m", memory = "320Mi" }
            limits   = { memory = "512Mi" }
          }
        }
        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map_v1.marquez[0].metadata[0].name
          }
        }
      }
    }
  }
  wait_for_rollout = true
  timeouts {
    create = "10m"
    update = "10m"
  }
  depends_on = [kubernetes_stateful_set_v1.postgres]
}

resource "kubernetes_service_v1" "marquez" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "marquez"
    namespace = kubernetes_namespace_v1.obs[0].metadata[0].name
  }
  spec {
    selector = { app = "marquez" }
    port {
      name        = "api"
      port        = 5000
      target_port = 5000
    }
  }
}

resource "kubernetes_deployment_v1" "marquez_web" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "marquez-web"
    namespace = kubernetes_namespace_v1.obs[0].metadata[0].name
    labels    = { app = "marquez-web" }
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "marquez-web" }
    }
    template {
      metadata {
        labels = { app = "marquez-web" }
      }
      spec {
        container {
          name  = "web"
          image = var.marquez_web_image
          env {
            name  = "MARQUEZ_HOST"
            value = "marquez"
          }
          env {
            name  = "MARQUEZ_PORT"
            value = "5000"
          }
          env {
            name  = "WEB_PORT"
            value = "3000"
          }
          port {
            name           = "web"
            container_port = 3000
          }
          security_context {
            allow_privilege_escalation = false
          }
          resources {
            requests = { cpu = "20m", memory = "96Mi" }
            limits   = { memory = "256Mi" }
          }
        }
      }
    }
  }
  wait_for_rollout = false # the UI is optional; the E2E reads lineage from the API
  depends_on       = [kubernetes_deployment_v1.marquez]
}

resource "kubernetes_service_v1" "marquez_web" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "marquez-web"
    namespace = kubernetes_namespace_v1.obs[0].metadata[0].name
  }
  spec {
    selector = { app = "marquez-web" }
    port {
      name        = "web"
      port        = 3000
      target_port = 3000
    }
  }
}
