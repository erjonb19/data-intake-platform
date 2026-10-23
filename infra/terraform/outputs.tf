output "kubeconfig_path" {
  description = "Kubeconfig for the kind cluster (gitignored)."
  value       = local.kubeconfig_path
}

output "airflow_url" {
  value = "http://localhost:${var.host_ports.airflow}"
}

output "airflow_admin_user" {
  value = "admin"
}

output "airflow_admin_password" {
  value     = local.pw["airflow_admin"]
  sensitive = true
}

output "minio_api_url" {
  value = "http://localhost:${var.host_ports.minio_api}"
}

output "minio_console_url" {
  value = "http://localhost:${var.host_ports.minio_console}"
}

output "minio_root_user" {
  value = local.minio_user
}

output "minio_root_password" {
  value     = local.pw["minio_root"]
  sensitive = true
}

output "warehouse_dsn" {
  description = "Host-side DSN for the warehouse database."
  value       = "postgresql://warehouse:${local.pw["pg_warehouse"]}@localhost:${var.host_ports.postgres}/warehouse"
  sensitive   = true
}

output "cohort_reader_dsn" {
  description = "Host-side DSN for the cohort service's read-only role (R7; Synthea builds only)."
  value       = "postgresql://dip_cohort_reader:${local.pw["pg_cohort_reader"]}@localhost:${var.host_ports.postgres}/warehouse"
  sensitive   = true
}

output "profiles" {
  value = var.profiles
}

output "sftp_vendor_keys" {
  description = "Each SFTP account's private key (OpenSSH), for the e2e and for trying the channel by hand."
  value       = { for a in local.sftp_accounts : a => tls_private_key.sftp_vendor[a].private_key_openssh }
  sensitive   = true
}

output "sftp_host_key" {
  description = "The SFTP server's public host key, to pin in known_hosts."
  value       = tls_private_key.sftp_host.public_key_openssh
}
