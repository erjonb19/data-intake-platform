#!/usr/bin/env bash
# Uses the repo's tracked hooks (.githooks/), so the pre-commit check runs the same
# scripts/check_no_mimic_data.sh that CI runs.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true
echo "git hooks enabled from .githooks/"
