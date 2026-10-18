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

output "profiles" {
  value = var.profiles
}
