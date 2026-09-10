#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

uv sync --frozen
uv run --frozen --no-sync python scripts/galaxy_dependencies.py
uv run --frozen --no-sync python scripts/dependencies.py verify
