# The nightly warehouse backup (Stage 14, 14.1): a CronJob in dip-data that pg_dumps the
# warehouse database into the MinIO bucket `backups`, keeps the newest var.backup_keep (7)
# dumps and records each run in ops.backup (dip/backup.py). The BackupMissing alert
# (observability/prometheus/rules.yml) fires when no run has succeeded in 26 hours.
# `python -m dip backup now` runs it on demand; docs/backups.md has the restore notes.
# Switched off with backups_enabled = false (the bucket stays: it costs nothing empty).
#
# One pod, two steps, sharing an emptyDir:
#   dump    (init container, var.postgres_image) pg_dump --format=custom, then
#           pg_restore --list to check the archive reads back. The Postgres image's own
#           pg_dump always matches the server's major version, which a client installed
#           anywhere else would not. It exits 0 even when the dump fails and leaves the
#           status and pg_dump's stderr in files, so a failed dump is still recorded
#   upload  (the Airflow image, which has boto3 and psycopg2 and the dip package, as the
#           platform check uses it) records the run, uploads, prunes and marks the row.
#           Not `mc` copied out of the Chainguard minio-client image: that image has no
#           shell to copy with, and the retention and the ops.backup row are then Python
#           with unit tests (tests/unit/test_backup.py) instead of shell
#
# Least privilege:
#   warehouse  the dip_backup login: pg_read_all_data (read everything, write nothing),
#              plus INSERT and UPDATE on ops.backup alone (postgres-roles.sql,
#              ops.grant_backup() in dip/sql/warehouse.sql)
#   MinIO      its own user and policy: list, put and delete in `backups`, nothing else
#              (it cannot read a dump back; the restore in 14.2 is a person's)
#   Kubernetes no service-account token mounted: the job calls no Kubernetes API
#
# Memory: the dump step requests 64Mi (limit 256Mi), the upload step 128Mi (limit 384Mi);
# they run one after the other, so the pod requests 128Mi, for a few minutes a night.

locals {
  backup_minio_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
        Resource = ["arn:aws:s3:::backups/*"]
      },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = ["arn:aws:s3:::backups"] },
    ]
  })
  # Versioned names, as for the intake API's user (intake_api.tf): mc refuses to attach a
  # policy that is already attached.
  backup_minio_suffix = substr(sha256("24.13:${local.backup_minio_policy}"), 0, 8) # 24.13: new Job spec, new name
  backup_minio_user   = "warehouse-backup-${local.backup_minio_suffix}"
}

resource "kubernetes_config_map_v1" "backup_minio_policy" {
  count = var.backups_enabled ? 1 : 0
  metadata {
    name      = "warehouse-backup-minio-policy"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = { "policy.json" = local.backup_minio_policy }
}

# The Chainguard mc image has no shell: one mc command per container, run in order.
resource "kubernetes_job_v1" "backup_minio_user" {
  count = var.backups_enabled ? 1 : 0
  metadata {
    name      = "minio-warehouse-backup-${local.backup_minio_suffix}"
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
            "1-policy" = ["admin", "policy", "create", "local", "warehouse-backup-${local.backup_minio_suffix}", "/policy/policy.json"]
            "2-user"   = ["admin", "user", "add", "local", local.backup_minio_user, "$(NEW_USER_PASSWORD)"]
          }
          content {
            name  = "mc-${init_container.key}"
            image = var.minio_client_image
            args  = init_container.value
            env {
              name = "NEW_USER_PASSWORD"
              value_from {
                secret_key_ref {
                  name = kubernetes_secret_v1.minio_user_passwords.metadata[0].name
                  key  = "backup"
                }
              }
            }
            env {
              name = "MC_HOST_local"
              value_from {
                secret_key_ref {
                  name = kubernetes_secret_v1.minio_admin.metadata[0].name
                  key  = "MC_HOST_local"
                }
              }
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
          args = ["admin", "policy", "attach", "local", "warehouse-backup-${local.backup_minio_suffix}",
          "--user", local.backup_minio_user]
          env {
            name = "MC_HOST_local"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.minio_admin.metadata[0].name
                key  = "MC_HOST_local"
              }
            }
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
            name = kubernetes_config_map_v1.backup_minio_policy[0].metadata[0].name
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

# The dump step's connection (libpq's environment), and nothing of MinIO.
resource "kubernetes_secret_v1" "backup_postgres" {
  count = var.backups_enabled ? 1 : 0
  metadata {
    name      = "dip-backup-postgres"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    PGHOST            = local.postgres_host
    PGPORT            = "5432"
    PGDATABASE        = "warehouse"
    PGUSER            = "dip_backup"
    PGPASSWORD        = local.pw["pg_backup"]
    PGCONNECT_TIMEOUT = "10"
  }
}

# The upload step's: the same login (to write ops.backup) and the backup's MinIO user.
resource "kubernetes_secret_v1" "backup" {
  count = var.backups_enabled ? 1 : 0
  metadata {
    name      = "dip-backup"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    DIP_WAREHOUSE_DSN = "postgresql://dip_backup:${local.pw["pg_backup"]}@${local.postgres_host}:5432/warehouse"
    DIP_S3_ENDPOINT   = "http://${local.minio_host}:9000"
    DIP_S3_ACCESS_KEY = local.backup_minio_user
    DIP_S3_SECRET_KEY = local.pw["minio_backup"]
    DIP_BACKUP_BUCKET = "backups"
  }
}

resource "kubernetes_cron_job_v1" "backup" {
  count = var.backups_enabled ? 1 : 0
  metadata {
    name      = "warehouse-backup"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    schedule           = var.backup_schedule
    concurrency_policy = "Forbid"
    # A laptop asleep at 02:15 runs it within the hour after, or skips to the next night
    # (and BackupMissing says so after 26 hours).
    starting_deadline_seconds     = 3600
    successful_jobs_history_limit = 3
    failed_jobs_history_limit     = 3
    job_template {
      metadata {}
      spec {
        # No retries: every attempt is an ops.backup row, and a failure should be seen
        # (the Job's status, the row's error), not hidden by a second try.
        backoff_limit           = 0
        active_deadline_seconds = 1800
        template {
          metadata {
            labels = { app = "warehouse-backup" }
          }
          spec {
            restart_policy                  = "Never"
            automount_service_account_token = false
            security_context {
              run_as_non_root = true
            }
            init_container {
              name  = "dump"
              image = var.postgres_image
              # Exits 0 on purpose: the upload step records a failed dump with its error.
              command = ["sh", "-c", <<-EOT
                set -u
                rm -f /backup/warehouse.dump /backup/status /backup/error
                date -u +%Y-%m-%dT%H:%M:%SZ > /backup/started_at
                if pg_dump --format=custom --lock-wait-timeout=60s \
                     --file=/backup/warehouse.dump 2>/backup/error \
                   && pg_restore --list /backup/warehouse.dump >/dev/null 2>>/backup/error; then
                  echo ok > /backup/status
                else
                  echo failed > /backup/status
                fi
                echo "dump: $(cat /backup/status), $(wc -c < /backup/warehouse.dump 2>/dev/null || echo 0) bytes"
              EOT
              ]
              env_from {
                secret_ref {
                  name = kubernetes_secret_v1.backup_postgres[0].metadata[0].name
                }
              }
              volume_mount {
                name       = "work"
                mount_path = "/backup"
              }
              resources {
                requests = { cpu = "50m", memory = "64Mi" }
                limits   = { memory = "256Mi" }
              }
              security_context {
                run_as_user                = 70 # postgres in the Alpine image
                allow_privilege_escalation = false
              }
            }
            container {
              name    = "upload"
              image   = "${var.airflow_image_repository}:${var.airflow_image_tag}"
              command = ["python", "-m", "dip.backup"]
              env_from {
                secret_ref {
                  name = kubernetes_secret_v1.backup[0].metadata[0].name
                }
              }
              env {
                name  = "DIP_BACKUP_DIR"
                value = "/backup"
              }
              env {
                name  = "DIP_BACKUP_KEEP"
                value = tostring(var.backup_keep)
              }
              volume_mount {
                name       = "work"
                mount_path = "/backup"
                read_only  = true
              }
              resources {
                requests = { cpu = "50m", memory = "128Mi" }
                limits   = { memory = "384Mi" }
              }
              security_context {
                allow_privilege_escalation = false
              }
            }
            volume {
              name = "work"
              empty_dir {
                size_limit = "2Gi"
              }
            }
          }
        }
      }
    }
  }
  depends_on = [
    kubernetes_job_v1.postgres_roles,
    kubernetes_job_v1.warehouse_init,
    kubernetes_job_v1.backup_minio_user,
  ]
}
