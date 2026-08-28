#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

git diff --check
if [[ "${FULL_SECRET_SCAN:-0}" == "1" ]]; then
  gitleaks git --redact --log-opts="--all" .
elif [[ -n "${CI_BASE_SHA:-}" && -n "${CI_HEAD_SHA:-}" ]]; then
  gitleaks git --redact --log-opts="${CI_BASE_SHA}..${CI_HEAD_SHA}" .
else
  gitleaks dir --redact .
fi

uv lock --check
uv run --frozen --no-sync python -m unittest discover -s tests/ci -p 'test_*.py'
uv run --frozen --no-sync python -m unittest discover -s tests/toolchain -p 'test_*.py'
uv run --frozen --no-sync python scripts/ci/repository_checks.py
uv run --frozen --no-sync yamllint --strict .
bash tests/operator/run-playbook-test.sh

shell_paths=()
while IFS= read -r -d '' shell_file; do
  shell_paths+=("$shell_file")
done < <(git ls-files -z --cached --others --exclude-standard -- '*.sh')
if [[ -f run-playbook ]]; then
  shell_paths+=("run-playbook")
fi

if ((${#shell_paths[@]} > 0)); then
  bash -n "${shell_paths[@]}"
  shellcheck --external-sources "${shell_paths[@]}"
fi

uv run --frozen --no-sync codespell
markdownlint-cli2 '**/*.md' '#.ansible' '#.venv' '#.tmp' '#.superpowers'

tracked_workflows=()
while IFS= read -r -d '' workflow_file; do
  tracked_workflows+=("$workflow_file")
done < <(
  git ls-files -z --cached -- \
    '.github/workflows/*.yaml' \
    '.github/workflows/*.yml'
)

if ((${#tracked_workflows[@]} > 0)); then
  actionlint
  uv run --frozen --no-sync zizmor .github/workflows
fi
