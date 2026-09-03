#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
CSV_FILE="$SCRIPT_DIR/manifest.csv"

TARGETS=()
DELETE_ONLY=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS] [ORBNAME1 [ORBNAME2 ...]]

Creates orbs based on entries in the manifest.csv file.

Arguments:
  ORBNAME       Optional. NAME (e.g. rocky9, debian12).
                If omitted, all orbs in the manifest will be processed.

Options:
  -d, --delete   Only delete the specified orbs (or all if none specified)
  -h, --help     Show this help message and exit

Examples:
  $(basename "$0")                     Recreate all orbs from manifest
  $(basename "$0") rocky9              Recreate only 'rocky9'
  $(basename "$0") debian12 debian13   Recreate specific orbs
  $(basename "$0") --delete            Delete all orbs from manifest
  $(basename "$0") --delete rocky9     Delete only 'rocky9'
EOF
  exit 0
}

is_target() {
  local name="$1"
  [[ ${#TARGETS[@]} -eq 0 ]] && return 0
  for t in "${TARGETS[@]}"; do
    [[ "$t" == "$name" ]] && return 0
  done
  return 1
}

process_orb() {
  local name="$1"
  local distro="$2"
  local version="$3"
  local arch="$4"
  local user="$5"

  is_target "$name" || return

  local image
  [[ -n "$version" ]] && image="${distro}:${version}" || image="${distro}"

  orb delete --force "$name" >/dev/null 2>&1
  [[ $DELETE_ONLY -eq 0 ]] && orb create --user "$user" --arch "$arch" "$image" "$name"
}

main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help)
        usage
        ;;
      -d|--delete)
        DELETE_ONLY=1
        shift
        while [[ $# -gt 0 && "$1" != -* ]]; do TARGETS+=("$1")
          shift
        done
        ;;
      -*)
        echo "Unknown option: $1"
        usage
        ;;
      *)
        TARGETS+=("$1")
        shift
        ;;
    esac
  done

  tail -n +2 "$CSV_FILE" | while IFS=, read -r name distro version arch user; do
    process_orb "$name" "$distro" "$version" "$arch" "$user"
  done
}

main "$@"
