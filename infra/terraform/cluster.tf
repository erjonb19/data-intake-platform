locals {
  kubeconfig_path = abspath("${path.root}/../../.kube/kind-${var.cluster_name}.yaml")

  # NodePort inside the cluster -> host port on 127.0.0.1.
  node_ports = {
    airflow       = 30080
    minio_api     = 30900
    minio_console = 30901
    postgres      = 30432
  }
}

resource "kind_cluster" "this" {
  name            = var.cluster_name
  node_image      = var.node_image
  wait_for_ready  = true
  kubeconfig_path = local.kubeconfig_path

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"

      dynamic "extra_port_mappings" {
        for_each = local.node_ports
        content {
          container_port = extra_port_mappings.value
          host_port      = var.host_ports[extra_port_mappings.key]
          listen_address = "127.0.0.1"
          protocol       = "TCP"
        }
      }
    }
  }
}

provider "kubernetes" {
  host                   = kind_cluster.this.endpoint
  client_certificate     = kind_cluster.this.client_certificate
  client_key             = kind_cluster.this.client_key
  cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
}

provider "helm" {
  kubernetes = {
    host                   = kind_cluster.this.endpoint
    client_certificate     = kind_cluster.this.client_certificate
    client_key             = kind_cluster.this.client_key
    cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
  }
}

# 24.15: Pod Security Admission on every namespace the platform creates.
# - enforce = baseline: the API server refuses privileged pods, host namespaces, host paths
#   and added capabilities.
# - warn and audit = restricted: every pod that is not yet at the restricted level is
#   reported at admission. The kind E2E prints that list (`kubectl label --dry-run=server`,
#   tests/e2e/pod_security.py).
# dip-airflow enforces restricted (24.15 part 2): the Airflow chart's pods, its Jobs, the
# KubernetesExecutor's task pods (infra/helm-values/airflow.yaml) and the proof workflow's
# `kubectl run` pods are all at that level. dip-data stays at baseline for the SFTP server,
# which needs root to chroot its users. dip-obs stays at baseline for Marquez's two
# upstream images, which run as root. Prometheus and Grafana there are at restricted.
locals {
  pod_security_labels = {
    "pod-security.kubernetes.io/enforce"         = "baseline"
    "pod-security.kubernetes.io/enforce-version" = "v1.35"
    "pod-security.kubernetes.io/warn"            = "restricted"
    "pod-security.kubernetes.io/warn-version"    = "v1.35"
    "pod-security.kubernetes.io/audit"           = "restricted"
    "pod-security.kubernetes.io/audit-version"   = "v1.35"
  }
}

resource "kubernetes_namespace_v1" "data" {
  metadata {
    name   = "dip-data"
    labels = merge({ "app.kubernetes.io/part-of" = "data-intake-platform" }, local.pod_security_labels)
  }
}

resource "kubernetes_namespace_v1" "airflow" {
  metadata {
    name = "dip-airflow"
    labels = merge({ "app.kubernetes.io/part-of" = "data-intake-platform" }, local.pod_security_labels,
    { "pod-security.kubernetes.io/enforce" = "restricted" })
  }
}
