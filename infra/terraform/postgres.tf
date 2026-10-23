# One shared Postgres for Airflow metadata, the warehouse, and (Stage 3) Marquez.
# Separate databases and roles; see PLAN.md section 5 for why it is shared.

resource "kubernetes_secret_v1" "postgres" {
  metadata {
    name      = "postgres-credentials"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    POSTGRES_PASSWORD      = local.pw["pg_superuser"]
    AIRFLOW_DB_PASSWORD    = local.pw["pg_airflow"]
    WAREHOUSE_DB_PASSWORD  = local.pw["pg_warehouse"]
    MARQUEZ_DB_PASSWORD    = local.pw["pg_marquez"]
    COHORT_READER_PASSWORD = local.pw["pg_cohort_reader"]
    METRICS_PASSWORD       = local.pw["pg_metrics"]
    MCP_PASSWORD           = local.pw["pg_mcp"]
  }
}

resource "kubernetes_config_map_v1" "postgres_init" {
  metadata {
    name      = "postgres-init"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    "01-databases.sh" = file("${path.module}/files/postgres-init.sh")
  }
}

resource "kubernetes_stateful_set_v1" "postgres" {
  metadata {
    name      = "postgres"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
    labels    = { app = "postgres" }
  }

  spec {
    service_name = "postgres"
    replicas     = 1

    selector {
      match_labels = { app = "postgres" }
    }

    template {
      metadata {
        labels = { app = "postgres" }
        annotations = {
          # Roll the pod if the init script changes (only matters on a fresh volume).
          "dip/init-sha" = sha256(file("${path.module}/files/postgres-init.sh"))
        }
      }

      spec {
        container {
          name  = "postgres"
          image = var.postgres_image

          port {
            name           = "postgres"
            container_port = 5432
          }

          env {
            name  = "POSTGRES_USER"
            value = "postgres"
          }
          env {
            name  = "PGDATA"
            value = "/var/lib/postgresql/data/pgdata"
          }
          env_from {
            secret_ref {
              name = kubernetes_secret_v1.postgres.metadata[0].name
            }
          }

          volume_mount {
            name       = "data"
            mount_path = "/var/lib/postgresql/data"
          }
          volume_mount {
            name       = "init"
            mount_path = "/docker-entrypoint-initdb.d"
            read_only  = true
          }

          readiness_probe {
            exec {
              command = ["pg_isready", "-U", "postgres"]
            }
            period_seconds = 5
          }

          resources {
            requests = { cpu = "100m", memory = "256Mi" }
            limits   = { memory = "768Mi" }
          }
        }

        volume {
          name = "init"
          config_map {
            name         = kubernetes_config_map_v1.postgres_init.metadata[0].name
            default_mode = "0555"
          }
        }
      }
    }

    volume_claim_template {
      metadata {
        name = "data"
      }
      spec {
        access_modes = ["ReadWriteOnce"]
        resources {
          requests = { storage = "5Gi" }
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

# Roles added after a cluster was first created never reach it through the init script
# (it runs only on a fresh volume), so this Job applies them on every apply, idempotently:
# terraform_data.every_apply changes on each apply, which replaces (reruns) the Job. That
# also restores a role someone dropped by hand. Plans therefore always show this Job.
resource "terraform_data" "every_apply" {
  triggers_replace = timestamp()
}

resource "kubernetes_config_map_v1" "postgres_roles" {
  metadata {
    name      = "postgres-roles"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    "roles.sql"     = file("${path.module}/files/postgres-roles.sql")
    "password.psql" = file("${path.module}/files/postgres-roles-password.psql")
  }
}

resource "kubernetes_job_v1" "postgres_roles" {
  metadata {
    name      = "postgres-roles"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    backoff_limit = 10 # retries until Postgres accepts connections
    template {
      metadata {}
      spec {
        restart_policy = "Never"
        container {
          name    = "psql"
          image   = var.postgres_image
          command = ["psql", "-v", "ON_ERROR_STOP=1", "-f", "/sql/roles.sql", "-f", "/sql/password.psql"]
          env {
            name  = "PGHOST"
            value = kubernetes_service_v1.postgres.metadata[0].name
          }
          env {
            name  = "PGUSER"
            value = "postgres"
          }
          env {
            name = "PGPASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgres.metadata[0].name
                key  = "POSTGRES_PASSWORD"
              }
            }
          }
          env {
            name = "COHORT_READER_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgres.metadata[0].name
                key  = "COHORT_READER_PASSWORD"
              }
            }
          }
          env {
            name = "METRICS_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgres.metadata[0].name
                key  = "METRICS_PASSWORD"
              }
            }
          }
          env {
            name = "MCP_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgres.metadata[0].name
                key  = "MCP_PASSWORD"
              }
            }
          }
          volume_mount {
            name       = "sql"
            mount_path = "/sql"
            read_only  = true
          }
        }
        volume {
          name = "sql"
          config_map {
            name = kubernetes_config_map_v1.postgres_roles.metadata[0].name
          }
        }
      }
    }
  }
  wait_for_completion = true
  timeouts {
    create = "5m"
  }
  depends_on = [kubernetes_stateful_set_v1.postgres]
  lifecycle {
    replace_triggered_by = [terraform_data.every_apply, kubernetes_config_map_v1.postgres_roles]
  }
}

resource "kubernetes_service_v1" "postgres" {
  metadata {
    name      = "postgres"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    selector = { app = "postgres" }
    port {
      name        = "postgres"
      port        = 5432
      target_port = 5432
    }
  }
}

# Host access for local debugging (127.0.0.1:15432 by default).
resource "kubernetes_service_v1" "postgres_nodeport" {
  metadata {
    name      = "postgres-nodeport"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    type     = "NodePort"
    selector = { app = "postgres" }
    port {
      name        = "postgres"
      port        = 5432
      target_port = 5432
      node_port   = local.node_ports.postgres
    }
  }
}
