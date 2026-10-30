variable "cluster_name" {
  description = "Name of the kind cluster."
  type        = string
  default     = "dip"
}

variable "node_image" {
  description = "kind node image, pinned by digest. v1.35 stays within kubectl's +/-1 minor version skew."
  type        = string
  default     = "kindest/node:v1.35.8@sha256:07b2536e30b803ed61d1677a79df6115f798ce64c80f9e22f6ed45afd09323c0"
}

variable "profiles" {
  description = "Stack profiles to deploy. 'core' is always required; 'observability' adds Prometheus, Grafana and the metrics exporter (observability.tf)."
  type        = list(string)
  default     = ["core"]

  validation {
    condition     = contains(var.profiles, "core") && alltrue([for p in var.profiles : contains(["core", "observability"], p)])
    error_message = "profiles must include \"core\" and may only contain \"core\" and \"observability\"."
  }
}

variable "host_ports" {
  description = "Host ports (bound to 127.0.0.1 only) mapped to cluster NodePorts."
  type = object({
    airflow       = number
    minio_api     = number
    minio_console = number
    postgres      = number
  })
  default = {
    airflow       = 8080
    minio_api     = 9000
    minio_console = 9001
    postgres      = 15432
  }
}

variable "airflow_chart_version" {
  description = "Official Apache Airflow Helm chart version."
  type        = string
  default     = "1.22.0"
}

variable "airflow_image_repository" {
  description = "Platform Airflow image (built from images/airflow and loaded into kind)."
  type        = string
  default     = "dip-airflow"
}

variable "airflow_image_tag" {
  description = "Content-addressed tag written by scripts/up.sh into image.auto.tfvars.json."
  type        = string
  default     = "dev"
}

variable "ocr_image_tag" {
  description = "OCR task image (images/ocr) tag, written by scripts/up.sh; empty: none built."
  type        = string
  default     = ""
}

variable "intake_api_image_repository" {
  description = "Intake API image (built from images/intake-api and loaded into kind)."
  type        = string
  default     = "dip-intake-api"
}

variable "intake_api_image_tag" {
  description = "Content-addressed tag written by scripts/up.sh into image.auto.tfvars.json."
  type        = string
  default     = "dev"
}

variable "sftp_image" {
  description = "SFTP server for the vendor channel (atmoz/sftp, OpenSSH on Debian), pinned by digest."
  type        = string
  default     = "atmoz/sftp:debian@sha256:f308cffe27242286d1e93a548dc5a581223b963e9b044d9e452594ef30dd05bd"
}

variable "postgres_image" {
  description = "Postgres image shared by Airflow metadata, the warehouse, and Marquez."
  type        = string
  default     = "postgres:17.6-alpine"
}

# Official MinIO images are no longer published to Docker Hub or quay.io.
# Chainguard builds MinIO from upstream source (AGPLv3); pinned by digest.
variable "minio_image" {
  description = "MinIO server image, pinned by digest."
  type        = string
  default     = "cgr.dev/chainguard/minio@sha256:709099319add48dfb44aa596a7070d3ddd8dd04d7ae881765719aec29df9e510"
}

variable "minio_client_image" {
  description = "MinIO client (mc) image, pinned by digest."
  type        = string
  default     = "cgr.dev/chainguard/minio-client@sha256:197566c04c427de33199b236cd4a0712df1dfc0e609f685432163eaff17b62f7"
}

variable "minio_buckets" {
  description = "Buckets created at deploy time."
  type        = list(string)
  default     = ["landing", "raw", "quarantine", "airflow-logs"]
}

variable "prometheus_chart_version" {
  description = "prometheus-community/prometheus chart (the plain chart, not kube-prometheus-stack); app v3.15.0."
  type        = string
  default     = "29.35.0"
}

variable "grafana_chart_version" {
  description = "grafana-community/grafana chart; app 13.2.3."
  type        = string
  default     = "13.3.1"
}

variable "marquez_image" {
  description = "Marquez API (OpenLineage), observability profile."
  type        = string
  default     = "marquezproject/marquez:0.51.1"
}

variable "marquez_web_image" {
  description = "Marquez web UI, observability profile."
  type        = string
  default     = "marquezproject/marquez-web:0.51.1"
}

variable "mcp_image_repository" {
  description = "MCP server image (images/mcp), loaded into kind by scripts/up.sh."
  type        = string
  default     = "dip-mcp"
}

variable "mcp_image_tag" {
  description = "MCP server image tag, written by scripts/up.sh."
  type        = string
  default     = "build"
}

# Stage 9 (S9.1): a model key for the agent_chain DAG's pods. Both default to empty, and
# with neither set the DAG records every incident as skipped and stays green: the
# cluster never needs a paid key. Pass one with TF_VAR_anthropic_api_key (never in a
# tfvars file that is committed).
variable "anthropic_api_key" {
  description = "Anthropic key for the agent_chain DAG (empty: the chain is skipped)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "gemini_api_key" {
  description = "Gemini key for the agent_chain DAG, used when no Anthropic key is set."
  type        = string
  default     = ""
  sensitive   = true
}
