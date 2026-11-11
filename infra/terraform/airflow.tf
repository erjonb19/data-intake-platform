locals {
  airflow_ns = kubernetes_namespace_v1.airflow.metadata[0].name

  airflow_secrets = {
    "airflow-metadata"       = { connection = "postgresql://airflow:${local.pw["pg_airflow"]}@${local.postgres_host}:5432/airflow" }
    "airflow-fernet-key"     = { "fernet-key" = local.airflow_fernet_key }
    "airflow-api-secret-key" = { "api-secret-key" = local.pw["airflow_api_secret"] }
    "airflow-jwt-secret"     = { "jwt-secret" = local.pw["airflow_jwt_secret"] }
    "dip-deid"               = { key = local.pw["dip_deid_key"] }
    # SFTP channel: where the server is, its pinned host key, and the platform's login key.
    "dip-sftp" = {
      host     = local.sftp_host
      host_key = tls_private_key.sftp_host.public_key_openssh
      key      = tls_private_key.sftp_platform.private_key_openssh
      accounts = join(",", local.sftp_accounts)
    }

    # Stage 9 (S9.1): the agent_chain DAG's pod environment (dags/agent_chain.py). The
    # in-cluster MCP URL, the incident and triage agents' keys, and a model key if one
    # was passed to Terraform. Only that DAG's pods mount it (env_from in its
    # pod_override); the chart's `secret` list does not include it.
    "dip-agent-chain" = {
      DIP_MCP_URL      = "http://mcp.${kubernetes_namespace_v1.data.metadata[0].name}.svc.cluster.local:8765/mcp"
      DIP_INCIDENT_KEY = random_password.mcp_incident_key.result
      DIP_TRIAGE_KEY   = random_password.mcp_triage_key.result
      # Stage 10 (S10.6): the intake team (dip/agents/intake_runner.py).
      DIP_PROFILE_KEY      = random_password.mcp_team_key["profile"].result
      DIP_AUTHOR_KEY       = random_password.mcp_team_key["contract_author"].result
      DIP_VALIDATOR_KEY    = random_password.mcp_team_key["validator"].result
      DIP_REVIEWER_KEY     = random_password.mcp_team_key["reviewer"].result
      DIP_ORCHESTRATOR_KEY = random_password.mcp_team_key["intake_orchestrator"].result
      ANTHROPIC_API_KEY    = var.anthropic_api_key
      GEMINI_API_KEY       = var.gemini_api_key
    }

    # Airflow connections, injected as AIRFLOW_CONN_* env vars into every Airflow pod
    # (including KubernetesExecutor task pods) via the chart's `secret` list.
    # The minio connection is Airflow's own MinIO user (24.2), not root: see below.
    "airflow-connections" = {
      minio = jsonencode({
        conn_type = "aws"
        login     = local.airflow_minio_user
        password  = local.pw["minio_airflow"]
        extra = {
          endpoint_url = "http://${local.minio_host}:9000"
          region_name  = "us-east-1"
        }
      })
      warehouse = "postgresql://warehouse:${local.pw["pg_warehouse"]}@${local.postgres_host}:5432/warehouse"
    }
  }
}

# 24.2: Airflow's own MinIO user. Every Airflow pod gets the minio connection, and task pods
# parse untrusted vendor files, so it may touch only the buckets Airflow uses: landing
# (intake, SFTP, onboarding), raw and quarantine (intake), airflow-logs (remote task logs).
# Not backups. ListAllMyBuckets lists names only (platform_smoke checks the buckets exist).
locals {
  airflow_minio_buckets = ["landing", "raw", "quarantine", "airflow-logs"]
  airflow_minio_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListAllMyBuckets"], Resource = ["arn:aws:s3:::*"] },
      { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"],
      Resource = [for b in local.airflow_minio_buckets : "arn:aws:s3:::${b}"] },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      Resource = [for b in local.airflow_minio_buckets : "arn:aws:s3:::${b}/*"] },
    ]
  })
  # Versioned like mcp.tf: a changed policy or password gets a fresh user and Job.
  airflow_minio_suffix = substr(sha256("${local.airflow_minio_policy}${local.pw["minio_airflow"]}"), 0, 8)
  airflow_minio_user   = "airflow-${local.airflow_minio_suffix}"
}

resource "kubernetes_config_map_v1" "airflow_minio_policy" {
  metadata {
    name      = "airflow-minio-policy"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = { "policy.json" = local.airflow_minio_policy }
}

# The Chainguard mc image has no shell: one mc command per container, run in order.
resource "kubernetes_job_v1" "airflow_minio_user" {
  metadata {
    name      = "minio-airflow-${local.airflow_minio_suffix}"
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
            "1-policy" = ["admin", "policy", "create", "local", local.airflow_minio_user, "/policy/policy.json"]
            "2-user"   = ["admin", "user", "add", "local", local.airflow_minio_user, local.pw["minio_airflow"]]
          }
          content {
            name  = "mc-${init_container.key}"
            image = var.minio_client_image
            args  = init_container.value
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
          args  = ["admin", "policy", "attach", "local", local.airflow_minio_user, "--user", local.airflow_minio_user]
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
        volume {
          name = "policy"
          config_map {
            name = kubernetes_config_map_v1.airflow_minio_policy.metadata[0].name
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

resource "kubernetes_secret_v1" "airflow" {
  for_each = local.airflow_secrets

  metadata {
    name      = each.key
    namespace = local.airflow_ns
  }
  data = each.value
}

# The official chart, committed in infra/helm-charts/ (how to update it is in its README).
# Fetching it from archive.apache.org at deploy time timed out on CI runners (incident
# 2026-11-06 in docs/incidents.md), so nothing here downloads it.
locals {
  airflow_chart        = "${path.module}/../helm-charts/airflow-${var.airflow_chart_version}.tgz"
  airflow_chart_sha512 = split(" ", trimspace(file("${local.airflow_chart}.sha512")))[0]
}

resource "helm_release" "airflow" {
  name      = "airflow"
  chart     = local.airflow_chart
  namespace = local.airflow_ns

  values = [file("${path.module}/../helm-values/airflow.yaml")]

  set = [
    { name = "images.airflow.repository", value = var.airflow_image_repository, type = "string" },
    # A content-addressed tag can be all digits ("873912234501"); without type = "string"
    # Helm reads it as a number and the chart's schema refuses it.
    { name = "images.airflow.tag", value = var.airflow_image_tag, type = "string" },
    # The PDF feed's intake task runs in the OCR image (dags/feed_intake.py).
    { name = "env[0].name", value = "DIP_OCR_IMAGE", type = "string" },
    { name = "env[0].value", value = var.ocr_image_tag == "" ? "" : "dip-ocr:${var.ocr_image_tag}", type = "string" },
    # Lineage to Marquez (S3.3) with the observability profile; without it, the emitter is
    # a no-op (dip/lineage.py).
    { name = "env[1].name", value = "OPENLINEAGE_URL", type = "string" },
    { name = "env[1].value", value = local.observability ? local.marquez_url : "", type = "string" },
    # Airflow's StatsD metrics (and the chart's statsd-exporter) only with Prometheus.
    { name = "statsd.enabled", value = tostring(local.observability), type = "auto" },
  ]

  set_sensitive = [
    { name = "createUserJob.defaultUser.password", value = local.pw["airflow_admin"] },
  ]

  # Migrations and user creation run as plain Jobs (useHelmHooks=false in the values
  # file) so that `wait` does not deadlock on post-install hooks.
  wait          = true
  wait_for_jobs = true
  timeout       = 900

  depends_on = [
    kubernetes_secret_v1.airflow,
    kubernetes_stateful_set_v1.postgres,
    kubernetes_service_v1.postgres,
    kubernetes_job_v1.minio_buckets,
    kubernetes_job_v1.airflow_minio_user,
  ]

  lifecycle {
    precondition {
      condition     = filesha512(local.airflow_chart) == local.airflow_chart_sha512
      error_message = "infra/helm-charts/airflow-${var.airflow_chart_version}.tgz does not match the upstream .sha512 beside it."
    }
  }
}
