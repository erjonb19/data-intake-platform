# Convenience targets. On Windows without make, run the scripts directly from Git Bash,
# e.g. `bash scripts/up.sh`.
.PHONY: prereqs up down smoke creds diagnostics lint fmt

prereqs:     ; bash scripts/prereqs.sh
up:          ; bash scripts/up.sh
down:        ; bash scripts/down.sh
smoke:       ; bash scripts/smoke.sh
creds:       ; bash scripts/creds.sh
diagnostics: ; bash scripts/diagnostics.sh

lint:
	ruff check .
	ruff format --check .
	terraform -chdir=infra/terraform fmt -check -recursive

fmt:
	ruff format .
	ruff check --fix .
	terraform -chdir=infra/terraform fmt -recursive
