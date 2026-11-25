# One shared Postgres for Airflow metadata, the warehouse, and (Stage 3) Marquez.
# Separate databases and roles; see PLAN.md section 5 for why it is shared.

resource "kubernetes_secret_v1" "postgres" {
  metadata {
    name      = "postgres-credentials"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    POSTGRES_PASSWORD       = local.pw["pg_superuser"]
    AIRFLOW_DB_PASSWORD     = local.pw["pg_airflow"]
    WAREHOUSE_DB_PASSWORD   = local.pw["pg_warehouse"]
    MARQUEZ_DB_PASSWORD     = local.pw["pg_marquez"]
    COHORT_READER_PASSWORD  = local.pw["pg_cohort_reader"]
    METRICS_PASSWORD        = local.pw["pg_metrics"]
    MCP_PASSWORD            = local.pw["pg_mcp"]
    APPROVER_PASSWORD       = local.pw["pg_approver"]
    PLATFORM_CHECK_PASSWORD = local.pw["pg_platform_check"]
    BACKUP_PASSWORD         = local.pw["pg_backup"]
    AGENT_RUNNER_PASSWORD   = local.pw["pg_agent_runner"]
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
          # 24.38: a new certificate or pg_hba.conf takes effect on a restart.
          "dip/tls-sha" = sha256(join("", [tls_locally_signed_cert.postgres.cert_pem,
          kubernetes_config_map_v1.postgres_hba.data["pg_hba.conf"]]))
        }
      }

      spec {
        # 24.15: as the image's postgres user (70), never root. The entrypoint runs
        # without root and skips its chown: the data directory already belongs to 70
        # (written by earlier pods), or initdb creates it in an empty volume.
        security_context {
          run_as_user     = 70
          run_as_group    = 70
          run_as_non_root = true
          # No fs_group: on a volume type that honours it, the kubelet would make PGDATA
          # group-writable, and Postgres refuses to start unless it is 0700 or 0750.
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }
        # 24.38: Postgres refuses a key file that others can read, and a Secret volume's
        # files belong to root without an fs_group (above). This copies the key into the
        # pod's own emptyDir, as user 70 and mode 0600.
        init_container {
          name    = "tls"
          image   = var.postgres_image
          command = ["sh", "-c", "install -m 0600 /tls-src/tls.key /tls/server.key && install -m 0644 /tls-src/tls.crt /tls/server.crt"]
          security_context {
            run_as_non_root            = true
            run_as_user                = 70
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities {
              drop = ["ALL"]
            }
            seccomp_profile {
              type = "RuntimeDefault"
            }
          }
          volume_mount {
            name       = "tls-src"
            mount_path = "/tls-src"
            read_only  = true
          }
          volume_mount {
            name       = "tls"
            mount_path = "/tls"
          }
        }
        container {
          name  = "postgres"
          image = var.postgres_image
          # 24.38: TLS on, and logins over TCP only with it (pg_hba.conf from tls.tf).
          args = ["postgres", "-c", "ssl=on", "-c", "ssl_cert_file=/tls/server.crt",
            "-c", "ssl_key_file=/tls/server.key", "-c", "ssl_min_protocol_version=TLSv1.2",
          "-c", "hba_file=/etc/dip-pg/pg_hba.conf"]
          security_context {
            run_as_non_root            = true
            run_as_user                = 70
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities {
              drop = ["ALL"]
            }
            seccomp_profile {
              type = "RuntimeDefault"
            }
          }

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
          # 24.15: the root filesystem is read-only; the socket directory and /tmp are not.
          volume_mount {
            name       = "run"
            mount_path = "/var/run/postgresql"
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
          volume_mount {
            name       = "tls"
            mount_path = "/tls"
            read_only  = true
          }
          volume_mount {
            name       = "hba"
            mount_path = "/etc/dip-pg"
            read_only  = true
          }

          readiness_probe {
            exec {
              command = ["pg_isready", "-U", "postgres"]
            }
            period_seconds = 5
          }
          # 13.1: generous, so a busy database (a restore, a dbt build) is never killed for
          # being slow: two minutes of failed checks before a restart.
          liveness_probe {
            exec {
              command = ["pg_isready", "-U", "postgres"]
            }
            initial_delay_seconds = 30
            period_seconds        = 20
            timeout_seconds       = 10
            failure_threshold     = 6
          }

          resources {
            requests = { cpu = "100m", memory = "256Mi" }
            limits   = { memory = "768Mi" }
          }
        }

        volume {
          name = "run"
          empty_dir {}
        }
        volume {
          name = "tmp"
          empty_dir {}
        }
        volume {
          name = "tls"
          empty_dir {
            medium = "Memory"
          }
        }
        volume {
          name = "tls-src"
          secret {
            secret_name = kubernetes_secret_v1.postgres_tls.metadata[0].name
            # Readable by user 70 only for the copy above; Postgres reads the 0600 copy.
            default_mode = "0444"
          }
        }
        volume {
          name = "hba"
          config_map {
            name = kubernetes_config_map_v1.postgres_hba.metadata[0].name
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
    # 24.14, run by the postgres-audit-owner Job (platform_check.tf) after the migrations.
    "audit-owner.sql" = file("${path.module}/files/postgres-audit-owner.sql")
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
          name  = "psql"
          image = var.postgres_image
          security_context {
            run_as_non_root            = true
            run_as_user                = 70
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities {
              drop = ["ALL"]
            }
            seccomp_profile {
              type = "RuntimeDefault"
            }
          }
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
          env {
            name = "APPROVER_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgres.metadata[0].name
                key  = "APPROVER_PASSWORD"
              }
            }
          }
          env {
            name = "PLATFORM_CHECK_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgres.metadata[0].name
                key  = "PLATFORM_CHECK_PASSWORD"
              }
            }
          }
          env {
            name = "BACKUP_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgres.metadata[0].name
                key  = "BACKUP_PASSWORD"
              }
            }
          }
          env {
            name = "AGENT_RUNNER_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.postgres.metadata[0].name
                key  = "AGENT_RUNNER_PASSWORD"
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
