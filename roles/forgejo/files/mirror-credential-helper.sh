#!/bin/sh
# core.askPass receives only a credential-free HTTPS prompt. Lookup reads the
# protected store through stdin/stdout; it never writes or erases declared inputs.
case "${1:-}" in
  Username\ for\ \'https://*) field=username ;;
  Password\ for\ \'https://*) field=password ;;
  *) exit 1 ;;
esac
mirror_url="${1#*\'}"
mirror_url="${mirror_url%%\'*}"
printf 'url=%s\n\n' "$mirror_url" |
  git credential-store --file=/etc/gitea/mirror-credentials get |
  while IFS= read -r line; do
    case "$line" in
      "$field="*) printf '%s\n' "${line#*=}" ;;
    esac
  done
