# Observability profile (Stage 3, S3.2): profiles = ["core", "observability"].
# Prometheus (the plain chart, no Alertmanager) scrapes Airflow's StatsD metrics (the
# Airflow chart's statsd-exporter, enabled only with this profile) and the warehouse
# exporter below; Grafana shows two dashboards. About 0.6 GB more (PLAN.md section 5), so
# the laptop runs it only when working on monitoring; CI's E2E runs it. ClusterIP only:
# reach Prometheus and Grafana with a port-forward (outputs show the commands).

locals {
  observability = contains(var.profiles, "observability")
  obs_ns        = "dip-obs"
}

resource "kubernetes_namespace_v1" "obs" {
  count = local.observability ? 1 : 0
  metadata {
    name   = local.obs_ns
    labels = { "app.kubernetes.io/part-of" = "data-intake-platform" }
  }
}

# The warehouse exporter (dip/metrics.py) as the read-only dip_metrics login, in the
# platform image (it has dip and psycopg2).
resource "kubernetes_secret_v1" "metrics" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "dip-metrics"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    DIP_METRICS_DSN = "postgresql://dip_metrics:${local.pw["pg_metrics"]}@${local.postgres_host}:5432/warehouse"
  }
}

resource "kubernetes_deployment_v1" "metrics" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "dip-metrics"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
    labels    = { app = "dip-metrics" }
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "dip-metrics" }
    }
    template {
      metadata {
        labels = { app = "dip-metrics" }
        annotations = {
          "dip/config-sha" = sha256(jsonencode(kubernetes_secret_v1.metrics[0].data))
        }
      }
      spec {
        container {
          name    = "exporter"
          image   = "${var.airflow_image_repository}:${var.airflow_image_tag}"
          command = ["python", "-m", "dip", "metrics-serve", "--port", "9187"]
          port {
            name           = "metrics"
            container_port = 9187
          }
          env_from {
            secret_ref {
              name = kubernetes_secret_v1.metrics[0].metadata[0].name
            }
          }
          readiness_probe {
            http_get {
              path = "/healthz"
              port = 9187
            }
            period_seconds = 10
          }
          security_context {
            run_as_non_root            = true
            run_as_user                = 50000
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
          }
          resources {
            requests = { cpu = "10m", memory = "48Mi" }
            limits   = { memory = "128Mi" }
          }
        }
      }
    }
  }
  wait_for_rollout = true
  depends_on       = [kubernetes_job_v1.postgres_roles]
}

resource "kubernetes_service_v1" "metrics" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "dip-metrics"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    selector = { app = "dip-metrics" }
    port {
      name        = "metrics"
      port        = 9187
      target_port = 9187
    }
  }
}

resource "helm_release" "prometheus" {
  count      = local.observability ? 1 : 0
  name       = "prometheus"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "prometheus"
  version    = var.prometheus_chart_version
  namespace  = kubernetes_namespace_v1.obs[0].metadata[0].name

  values = [
    file("${path.module}/../helm-values/prometheus.yaml"),
    yamlencode({
      serverFiles = {
        "alerting_rules.yml" = yamldecode(file("${path.module}/../../observability/prometheus/rules.yml"))
      }
    }),
  ]

  wait    = true
  timeout = 600
}

resource "kubernetes_secret_v1" "grafana_admin" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "grafana-admin"
    namespace = kubernetes_namespace_v1.obs[0].metadata[0].name
  }
  data = {
    "admin-user"     = "admin"
    "admin-password" = local.pw["grafana_admin"]
  }
}

resource "kubernetes_config_map_v1" "grafana_dashboards" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "grafana-dashboards"
    namespace = kubernetes_namespace_v1.obs[0].metadata[0].name
  }
  data = {
    for f in fileset("${path.module}/../../observability/grafana", "*.json") :
    f => file("${path.module}/../../observability/grafana/${f}")
  }
}

resource "helm_release" "grafana" {
  count      = local.observability ? 1 : 0
  name       = "grafana"
  repository = "https://grafana-community.github.io/helm-charts"
  chart      = "grafana"
  version    = var.grafana_chart_version
  namespace  = kubernetes_namespace_v1.obs[0].metadata[0].name

  values = [file("${path.module}/../helm-values/grafana.yaml")]
  set = [
    # Roll Grafana when a dashboard changes (it reads the provisioned files at start).
    { name = "podAnnotations.dip/dashboards-sha", value = sha256(jsonencode(kubernetes_config_map_v1.grafana_dashboards[0].data)), type = "string" },
  ]

  wait       = true
  timeout    = 600
  depends_on = [kubernetes_secret_v1.grafana_admin, helm_release.prometheus]
}
