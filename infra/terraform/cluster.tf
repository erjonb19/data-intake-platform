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

resource "kubernetes_namespace_v1" "data" {
  metadata {
    name   = "dip-data"
    labels = { "app.kubernetes.io/part-of" = "data-intake-platform" }
  }
}

resource "kubernetes_namespace_v1" "airflow" {
  metadata {
    name   = "dip-airflow"
    labels = { "app.kubernetes.io/part-of" = "data-intake-platform" }
  }
}
