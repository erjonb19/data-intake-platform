# 24.16: NetworkPolicies, ingress only. Each platform namespace denies every connection
# from another pod by default, then allows the flows the platform uses (mapped from the
# Terraform, Helm values, DAGs and scripts in 24.16's PR). A compromised task pod
# can no longer reach every service: an SFTP or intake-API pod cannot reach Postgres, a pod
# outside the platform's namespaces reaches nothing, and only Airflow's task pods reach
# SFTP and the MCP server.
#
# Traffic from outside the pod network is still allowed to every pod: the kubelet's probes
# and the NodePorts (Airflow, MinIO, Postgres on 127.0.0.1) arrive from the node's address,
# not a pod's. `kubectl port-forward` does not pass through NetworkPolicy at all.
#
# kind's own CNI (kindnet, kind v0.24 and later; CI runs v0.33) enforces NetworkPolicy. The
# E2E checks this (tests/e2e/network_policy.py): a pod outside the platform is refused, and
# an Airflow task pod gets through.

locals {
  pod_cidr         = "10.244.0.0/16" # kind's default podSubnet (cluster.tf sets none)
  node_pod_gateway = "10.244.0.1/32" # the one node's podCIDR is 10.244.0.0/24
  ns_label         = "kubernetes.io/metadata.name"
  np_namespaces = merge(
    { data = kubernetes_namespace_v1.data.metadata[0].name, airflow = kubernetes_namespace_v1.airflow.metadata[0].name },
    local.observability ? { obs = kubernetes_namespace_v1.obs[0].metadata[0].name } : {},
  )
}

# Every pod: nothing from other pods unless a policy below allows it; anything from outside
# the pod network (probes, NodePorts).
resource "kubernetes_network_policy_v1" "default_deny" {
  for_each = local.np_namespaces
  metadata {
    name      = "default-deny-pods"
    namespace = each.value
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress"]
    ingress {
      from {
        ip_block {
          cidr   = "0.0.0.0/0"
          except = [local.pod_cidr]
        }
      }
      # The node's own address on the pod network (the host end of every pod's veth, in
      # kind's single-node cluster): NodePort traffic and probes can arrive from it.
      from {
        ip_block {
          cidr = local.node_pod_gateway
        }
      }
    }
  }
}

# Airflow's own pods talk to each other (task pods to the api-server's Execution API,
# statsd, the scheduler), and Grafana, Prometheus and Marquez's web UI do the same in dip-obs.
resource "kubernetes_network_policy_v1" "same_namespace" {
  for_each = { for k, v in local.np_namespaces : k => v if k != "data" }
  metadata {
    name      = "same-namespace"
    namespace = each.value
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress"]
    ingress {
      from {
        pod_selector {}
      }
    }
  }
}

# --- dip-data ---------------------------------------------------------------------------

# Postgres: Airflow (metadata DB, the warehouse in every task, warehouse-init, platform
# check), Marquez, and the dip-data pods that use it (MCP, approvals, the metrics exporter,
# the backup, the roles Jobs). Not SFTP, MinIO or the intake API, which never connect.
resource "kubernetes_network_policy_v1" "postgres" {
  metadata {
    name      = "postgres"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    pod_selector {
      match_labels = { app = "postgres" }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "5432"
        protocol = "TCP"
      }
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = kubernetes_namespace_v1.airflow.metadata[0].name }
        }
      }
      from {
        pod_selector {
          match_expressions {
            key      = "app"
            operator = "NotIn"
            values   = ["sftp", "minio", "intake-api"]
          }
        }
      }
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = local.obs_ns }
        }
        pod_selector {
          match_labels = { app = "marquez" }
        }
      }
    }
  }
}

# MinIO's API: Airflow (its S3 connection, remote logs, every task), and the dip-data pods
# that use it (the intake API, MCP, the backup upload, the mc Jobs, and Postgres itself,
# which downloads a dump during `dip restore`). Not SFTP, approvals or the metrics exporter.
# The console (9001) has no in-cluster client: NodePort only.
resource "kubernetes_network_policy_v1" "minio" {
  metadata {
    name      = "minio"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    pod_selector {
      match_labels = { app = "minio" }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "9000"
        protocol = "TCP"
      }
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = kubernetes_namespace_v1.airflow.metadata[0].name }
        }
      }
      from {
        pod_selector {
          match_expressions {
            key      = "app"
            operator = "NotIn"
            values   = ["sftp", "approvals", "dip-metrics"]
          }
        }
      }
    }
  }
}

# SFTP: only Airflow's task pods (the sftp_sync DAG).
resource "kubernetes_network_policy_v1" "sftp" {
  metadata {
    name      = "sftp"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    pod_selector {
      match_labels = { app = "sftp" }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "22"
        protocol = "TCP"
      }
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = kubernetes_namespace_v1.airflow.metadata[0].name }
        }
        pod_selector {
          match_labels = { component = "worker" }
        }
      }
    }
  }
}

# The MCP server: Airflow's task pods (the agent chain and the intake team) and the proof
# workflow's `kubectl run` pods in dip-airflow (label `run`).
resource "kubernetes_network_policy_v1" "mcp" {
  metadata {
    name      = "mcp"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    pod_selector {
      match_labels = { app = "mcp" }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "8765"
        protocol = "TCP"
      }
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = kubernetes_namespace_v1.airflow.metadata[0].name }
        }
        pod_selector {
          match_labels = { component = "worker" }
        }
      }
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = kubernetes_namespace_v1.airflow.metadata[0].name }
        }
        pod_selector {
          match_expressions {
            key      = "run"
            operator = "Exists"
          }
        }
      }
    }
  }
}

# The intake API's /metrics and the warehouse exporter: Prometheus (dip-obs). The intake API
# also takes the proof workflow's load generator (`kubectl run intake-api-load`).
resource "kubernetes_network_policy_v1" "scraped" {
  metadata {
    name      = "scraped-by-prometheus"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    pod_selector {
      match_expressions {
        key      = "app"
        operator = "In"
        values   = ["intake-api", "dip-metrics"]
      }
    }
    policy_types = ["Ingress"]
    ingress {
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = local.obs_ns }
        }
      }
      from {
        pod_selector {
          match_labels = { run = "intake-api-load" }
        }
      }
    }
  }
}

# --- dip-airflow ------------------------------------------------------------------------

# The api-server: MCP's Airflow viewer (dag runs and task logs for the agents).
resource "kubernetes_network_policy_v1" "airflow_api_server" {
  metadata {
    name      = "api-server-from-mcp"
    namespace = kubernetes_namespace_v1.airflow.metadata[0].name
  }
  spec {
    pod_selector {
      match_labels = { component = "api-server" }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "8080"
        protocol = "TCP"
      }
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = kubernetes_namespace_v1.data.metadata[0].name }
        }
        pod_selector {
          match_labels = { app = "mcp" }
        }
      }
    }
  }
}

# statsd's exporter: Prometheus.
resource "kubernetes_network_policy_v1" "airflow_statsd" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "statsd-from-prometheus"
    namespace = kubernetes_namespace_v1.airflow.metadata[0].name
  }
  spec {
    pod_selector {
      match_labels = { component = "statsd" }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "9102"
        protocol = "TCP"
      }
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = local.obs_ns }
        }
      }
    }
  }
}

# --- dip-obs ----------------------------------------------------------------------------

# Marquez's API: OpenLineage events from Airflow's task pods (intake and dbt).
resource "kubernetes_network_policy_v1" "marquez" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "marquez-from-airflow"
    namespace = kubernetes_namespace_v1.obs[0].metadata[0].name
  }
  spec {
    pod_selector {
      match_labels = { app = "marquez" }
    }
    policy_types = ["Ingress"]
    ingress {
      ports {
        port     = "5000"
        protocol = "TCP"
      }
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = kubernetes_namespace_v1.airflow.metadata[0].name }
        }
      }
    }
  }
}

# The MCP server's read tools query Prometheus and Marquez.
resource "kubernetes_network_policy_v1" "obs_from_mcp" {
  count = local.observability ? 1 : 0
  metadata {
    name      = "from-mcp"
    namespace = kubernetes_namespace_v1.obs[0].metadata[0].name
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress"]
    ingress {
      from {
        namespace_selector {
          match_labels = { (local.ns_label) = kubernetes_namespace_v1.data.metadata[0].name }
        }
        pod_selector {
          match_labels = { app = "mcp" }
        }
      }
    }
  }
}
