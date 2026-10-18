# Stage 0 demo: foundations

**Goal:** one command brings up a real Kubernetes stack with Terraform, and a test proves
that Airflow's KubernetesExecutor, Postgres and MinIO all work together.

## 1. Bring it up (about 5 minutes on first run, about 1 minute after that)

```bash
bash scripts/prereqs.sh
bash scripts/up.sh
```

Show:
- Terraform creates the kind cluster first, then the image is built and loaded, then the
  rest of the stack deploys (28 resources).
- Running `bash scripts/up.sh` again changes nothing (`terraform plan` is clean).

## 2. Look around

```bash
export KUBECONFIG="$(terraform -chdir=infra/terraform output -raw kubeconfig_path)"
kubectl get pods -n dip-airflow
kubectl get pods -n dip-data
bash scripts/creds.sh
```

- Open http://localhost:8080 and log in as `admin`. The `platform_smoke` DAG is listed.
- Open http://localhost:9001 to see the MinIO buckets: `landing`, `raw`, `quarantine` and `airflow-logs`.

## 3. Prove it works

```bash
bash scripts/smoke.sh
```

Expected output:

```
check_minio      success  pod=platform-smoke-check-minio-...
check_warehouse  success  pod=platform-smoke-check-warehouse-...
report           success  pod=platform-smoke-report-...
==> reading task log back from remote storage
PASS: Stage 0 smoke test
```

What this shows:
- Each task ran in **its own pod** (different hostnames), which means KubernetesExecutor is working.
- Tasks reached MinIO and the warehouse database (schemas `bronze`, `silver`, `gold`
  and `ops`) using connections injected from Kubernetes Secrets.
- Task logs were read back **after the pods were deleted**, so remote logging to MinIO works.

While it runs, `kubectl get pods -n dip-airflow -w` shows the task pods starting and stopping.

## 4. CI

The `e2e` workflow on GitHub runs the same `up.sh` and `smoke.sh` on a fresh runner for
every push.

## 5. Tear down

```bash
bash scripts/down.sh
```
