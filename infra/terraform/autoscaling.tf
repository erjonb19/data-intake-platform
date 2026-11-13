# 13.4: autoscaling. metrics-server (a Helm release in kube-system) reports pod CPU, and two
# HorizontalPodAutoscalers scale the intake API and the MCP server from 1 to 3 pods on it.
# Both behind var.metrics_server_enabled: off, neither is installed. The two Deployments'
# `replicas` is left to the HPA (ignore_changes in intake_api.tf and mcp.tf), so an apply
# never scales a busy service back to one; for the same reason, turning the switch off after
# a scale-up leaves that count until it is scaled back by hand (see the variable).

resource "helm_release" "metrics_server" {
  count      = var.metrics_server_enabled ? 1 : 0
  name       = "metrics-server"
  repository = "https://kubernetes-sigs.github.io/metrics-server/"
  chart      = "metrics-server"
  version    = var.metrics_server_chart_version
  namespace  = "kube-system"

  values = [file("${path.module}/../helm-values/metrics-server.yaml")]

  wait    = true
  timeout = 300
}

locals {
  # name -> namespace of the Deployments that scale
  autoscaled = {
    "intake-api" = kubernetes_namespace_v1.data.metadata[0].name
    "mcp"        = kubernetes_namespace_v1.data.metadata[0].name
  }
}

resource "kubernetes_horizontal_pod_autoscaler_v2" "service" {
  for_each = var.metrics_server_enabled ? local.autoscaled : {}

  metadata {
    name      = each.key
    namespace = each.value
  }
  spec {
    min_replicas = 1
    max_replicas = 3
    scale_target_ref {
      api_version = "apps/v1"
      kind        = "Deployment"
      name        = each.key
    }
    metric {
      type = "Resource"
      resource {
        name = "cpu"
        target {
          type                = "Utilization"
          average_utilization = 70 # of the pod's CPU request (20m)
        }
      }
    }
    behavior {
      # Back to one pod a minute after the load ends (the default waits five), so the
      # scaling proof sees the way down too.
      scale_down {
        stabilization_window_seconds = 60
        select_policy                = "Max"
        policy {
          type           = "Pods"
          value          = 1
          period_seconds = 30
        }
      }
    }
  }

  depends_on = [helm_release.metrics_server]
}
