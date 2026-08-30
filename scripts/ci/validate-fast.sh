#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

uv run --frozen --no-sync python scripts/ci/candidate_validation.py

uv lock --check
uv run --frozen --no-sync python -m unittest discover -s tests/ci -p 'test_*.py'
uv run --frozen --no-sync python -m unittest discover -s tests/repository -p 'test_*.py'
uv run --frozen --no-sync python -m unittest discover -s tests/toolchain -p 'test_*.py'
uv run --frozen --no-sync python scripts/ci/repository_validation.py
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
markdownlint-cli2 '**/*.md' '#.ansible' '#.venv' '#.tmp'

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
