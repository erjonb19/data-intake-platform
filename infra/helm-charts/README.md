# Committed Helm charts

`airflow-1.22.0.tgz` is the official Apache Airflow Helm chart, release 1.22.0, byte for
byte as published at <https://archive.apache.org/dist/airflow/helm-chart/1.22.0/>.
`infra/terraform/airflow.tf` deploys it from this folder, so neither `scripts/up.sh` nor CI
downloads it. Downloading it on every deploy failed CI when archive.apache.org throttled
GitHub's runners (incident 2026-11-06, `docs/incidents.md`).

- **License.** Apache-2.0. The chart's own `LICENSE` and `NOTICE` (which also covers the
  Bitnami PostgreSQL chart vendored inside it) are copied unchanged in `airflow-license/`,
  and are also inside the `.tgz`.
- **Checksum.** `airflow-1.22.0.tgz.sha512` is Apache's checksum file, unchanged.
  Terraform refuses to deploy if the `.tgz` does not match it (a `precondition` on
  `helm_release.airflow`), and `tests/unit/test_helm_charts.py` checks the same in CI.
- **Signature.** Checked once when the file was added (2026-11-06): `gpg --verify` of
  Apache's `.asc` against <https://downloads.apache.org/airflow/KEYS> gave a good
  signature from an Airflow release manager's key (`B31140BBB84DC54D`).

## Updating the chart version

1. Pick the release from <https://airflow.apache.org/docs/helm-chart/stable/release_notes.html>
   and download it with its checksum and signature into an empty folder outside the repo:

   ```bash
   V=1.23.0
   base=https://archive.apache.org/dist/airflow/helm-chart/$V
   for f in airflow-$V.tgz airflow-$V.tgz.sha512 airflow-$V.tgz.asc; do curl -fsSLO --retry 5 "$base/$f"; done
   sha512sum -c airflow-$V.tgz.sha512
   curl -fsSLO https://downloads.apache.org/airflow/KEYS && gpg --import KEYS
   gpg --verify airflow-$V.tgz.asc airflow-$V.tgz   # must say "Good signature"
   ```

2. Copy `airflow-$V.tgz` and `airflow-$V.tgz.sha512` here, delete the old pair, and
   refresh `airflow-license/` from the new chart
   (`tar xzf airflow-$V.tgz airflow/LICENSE airflow/NOTICE`).
3. Set `airflow_chart_version` in `infra/terraform/variables.tf` to `$V`, and the chart
   version in the README's "What runs where" table.
4. Read the chart's release notes against `infra/helm-values/airflow.yaml`. The `upgrade`
   workflow runs on the PR (it touches `infra/`) and deploys the old version before
   upgrading to the new one.
