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

output "intake_api_keys" {
  description = "Each client's intake API key (Bearer token), for the e2e and for trying the API by hand."
  value       = { for name in keys(local.api_clients) : name => random_password.api_key[name].result }
  sensitive   = true
}

output "observability" {
  description = "How to reach Prometheus and Grafana (observability profile), or null."
  value = local.observability ? {
    prometheus = "kubectl -n ${local.obs_ns} port-forward svc/prometheus-server 9090:80"
    grafana    = "kubectl -n ${local.obs_ns} port-forward svc/grafana 3000:80 (user admin)"
    marquez    = "kubectl -n ${local.obs_ns} port-forward svc/marquez 5000:5000 (API)"
    lineage_ui = "kubectl -n ${local.obs_ns} port-forward svc/marquez-web 3001:3000"
  } : null
}

output "grafana_admin_password" {
  description = "Grafana's admin password (observability profile)."
  value       = local.pw["grafana_admin"]
  sensitive   = true
}

output "mcp_reader_key" {
  description = "The MCP Read tier's API key (agent `reader`): Authorization: Bearer <key>."
  value       = random_password.mcp_reader_key.result
  sensitive   = true
}

output "mcp_triage_key" {
  description = "The triage agent's MCP API key (agent `triage`): Authorization: Bearer <key>."
  value       = random_password.mcp_triage_key.result
  sensitive   = true
}

output "mcp_incident_key" {
  description = "The incident agent's MCP API key (agent `incident`), also in the agent_chain DAG's Secret."
  value       = random_password.mcp_incident_key.result
  sensitive   = true
}

output "approvals_token" {
  description = "The approvals page's bearer token (Stage 9, S9.2): paste it into the page once"
  value       = random_password.approvals_token.result
  sensitive   = true
}
