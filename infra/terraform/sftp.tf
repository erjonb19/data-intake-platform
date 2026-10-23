# SFTP channel (Stage 2, PLAN.md §8.6 S2.2): one key-only account per vendor (named after
# its pack), chrooted to its home, uploading into /upload/<feed>/. The sftp_sync DAG logs
# in to each account with the platform's own key and moves complete files to landing.
# All keys are generated here (tls provider), kept in local Terraform state (gitignored)
# and Kubernetes Secrets, and never committed. The host key is fixed too, so clients can
# pin it instead of trusting whatever key answers.

locals {
  sftp_accounts = ["healthcare", "finance"]
  sftp_host     = "sftp.${kubernetes_namespace_v1.data.metadata[0].name}.svc.cluster.local"
  # atmoz/sftp users.conf: name:password:uid:gid:dirs. No password: keys only.
  sftp_users_conf = join("\n", [
    for i, a in local.sftp_accounts : "${a}::${2001 + i}:100:upload"
  ])
}

resource "tls_private_key" "sftp_host" {
  algorithm = "ED25519"
}

resource "tls_private_key" "sftp_platform" {
  algorithm = "ED25519"
}

resource "tls_private_key" "sftp_vendor" {
  for_each  = toset(local.sftp_accounts)
  algorithm = "ED25519"
}

resource "kubernetes_secret_v1" "sftp_host_key" {
  metadata {
    name      = "sftp-host-key"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    ssh_host_ed25519_key = tls_private_key.sftp_host.private_key_openssh
  }
}

# Public keys only: each account accepts its vendor's key and the platform's key.
resource "kubernetes_config_map_v1" "sftp" {
  metadata {
    name      = "sftp-config"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = merge(
    { "users.conf" = local.sftp_users_conf },
    { for a in local.sftp_accounts : "${a}.vendor.pub" => tls_private_key.sftp_vendor[a].public_key_openssh },
    { "platform.pub" = tls_private_key.sftp_platform.public_key_openssh },
  )
}

resource "kubernetes_persistent_volume_claim_v1" "sftp" {
  metadata {
    name      = "sftp-data"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    access_modes = ["ReadWriteOnce"]
    resources {
      requests = { storage = "1Gi" }
    }
  }
  wait_until_bound = false
}

resource "kubernetes_deployment_v1" "sftp" {
  metadata {
    name      = "sftp"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
    labels    = { app = "sftp" }
  }

  spec {
    replicas = 1
    strategy {
      type = "Recreate" # RWO volume
    }
    selector {
      match_labels = { app = "sftp" }
    }
    template {
      metadata {
        labels = { app = "sftp" }
        annotations = {
          # Roll the pod when accounts or keys change: atmoz/sftp reads them at start.
          "dip/config-sha" = sha256(jsonencode(kubernetes_config_map_v1.sftp.data))
        }
      }
      spec {
        container {
          name  = "sftp"
          image = var.sftp_image
          port {
            name           = "ssh"
            container_port = 22
          }
          volume_mount {
            name       = "config"
            mount_path = "/etc/sftp/users.conf"
            sub_path   = "users.conf"
            read_only  = true
          }
          volume_mount {
            name       = "host-key"
            mount_path = "/etc/ssh/ssh_host_ed25519_key"
            sub_path   = "ssh_host_ed25519_key"
            read_only  = true
          }
          dynamic "volume_mount" {
            for_each = local.sftp_accounts
            content {
              name       = "config"
              mount_path = "/home/${volume_mount.value}/.ssh/keys/vendor.pub"
              sub_path   = "${volume_mount.value}.vendor.pub"
              read_only  = true
            }
          }
          dynamic "volume_mount" {
            for_each = local.sftp_accounts
            content {
              name       = "config"
              mount_path = "/home/${volume_mount.value}/.ssh/keys/platform.pub"
              sub_path   = "platform.pub"
              read_only  = true
            }
          }
          dynamic "volume_mount" {
            for_each = local.sftp_accounts
            content {
              name       = "data"
              mount_path = "/home/${volume_mount.value}/upload"
              sub_path   = volume_mount.value
            }
          }
          readiness_probe {
            tcp_socket {
              port = 22
            }
            period_seconds = 5
          }
          resources {
            requests = { cpu = "20m", memory = "16Mi" }
            limits   = { memory = "64Mi" }
          }
        }
        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map_v1.sftp.metadata[0].name
          }
        }
        volume {
          name = "host-key"
          secret {
            secret_name  = kubernetes_secret_v1.sftp_host_key.metadata[0].name
            default_mode = "0600"
          }
        }
        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.sftp.metadata[0].name
          }
        }
      }
    }
  }
  wait_for_rollout = true
}

resource "kubernetes_service_v1" "sftp" {
  metadata {
    name      = "sftp"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  spec {
    selector = { app = "sftp" }
    port {
      name        = "ssh"
      port        = 22
      target_port = 22
    }
  }
}
