# 24.38 (part 1): TLS for Postgres inside the cluster. One self-signed CA, made here, signs
# the server's certificate. Postgres turns ssl on and its pg_hba.conf takes TCP logins only
# over TLS, so no password or row crosses the pod network in the clear. libpq's default
# (sslmode=prefer) takes TLS whenever the server offers it, so the Python clients, psql and
# Airflow need no change; Marquez's JDBC URL asks for it (marquez.tf). Clients checking the
# certificate (verify-full, with this CA mounted in every pod) is part 2.

resource "tls_private_key" "ca" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_self_signed_cert" "ca" {
  private_key_pem       = tls_private_key.ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 24 * 365
  early_renewal_hours   = 24 * 30
  allowed_uses          = ["cert_signing", "crl_signing"]
  subject {
    common_name  = "dip local CA"
    organization = "data-intake-platform (local cluster)"
  }
}

resource "tls_private_key" "postgres" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_cert_request" "postgres" {
  private_key_pem = tls_private_key.postgres.private_key_pem
  subject {
    common_name = local.postgres_host
  }
  dns_names = [
    local.postgres_host,
    "postgres.${kubernetes_namespace_v1.data.metadata[0].name}",
    "postgres",
    "localhost", # the NodePort and port-forwards, from the host
  ]
  ip_addresses = ["127.0.0.1"]
}

resource "tls_locally_signed_cert" "postgres" {
  cert_request_pem      = tls_cert_request.postgres.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = 24 * 90
  early_renewal_hours   = 24 * 14
  allowed_uses          = ["digital_signature", "key_encipherment", "server_auth"]
}

resource "kubernetes_secret_v1" "postgres_tls" {
  metadata {
    name      = "postgres-tls"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    "tls.crt" = tls_locally_signed_cert.postgres.cert_pem
    "tls.key" = tls_private_key.postgres.private_key_pem
  }
}

# Every TCP login over TLS, with a password; the socket inside the pod (the image's init
# scripts, pg_isready) as before. A login without TLS matches no line and is refused.
resource "kubernetes_config_map_v1" "postgres_hba" {
  metadata {
    name      = "postgres-hba"
    namespace = kubernetes_namespace_v1.data.metadata[0].name
  }
  data = {
    "pg_hba.conf" = <<-EOT
      # TYPE    DATABASE  USER  ADDRESS  METHOD
      local     all       all            trust
      hostssl   all       all   all      scram-sha-256
    EOT
  }
}
