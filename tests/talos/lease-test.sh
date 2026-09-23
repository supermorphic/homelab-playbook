#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=/dev/null
source "$repo_root/roles/talos_lifecycle/files/lib/lease.sh"

[[ "$TEST_LEASE_NAMESPACE" == flux-system ]]
[[ "$TEST_LEASE_NAME" == homelab-test-run-lock ]]
[[ "$TEST_LEASE_DURATION_SECONDS" == 90 ]]
[[ "$TEST_LEASE_RENEW_INTERVAL_SECONDS" == 30 ]]

now=2026-09-23T12:00:00.000000Z
expires="$(lease_expiry_epoch 2026-09-23T11:59:00.000000Z 90)"
((expires > 0))
lease_record_valid_holder '{"spec":{"holderIdentity":"holder-a","leaseDurationSeconds":90,"renewTime":"'"$now"'"}}' holder-a
if lease_record_valid_holder '{"spec":{"holderIdentity":"holder-b","leaseDurationSeconds":90,"renewTime":"'"$now"'"}}' holder-a; then
  printf '%s\n' 'foreign holder accepted' >&2
  exit 1
fi
printf '%s\n' 'Talos Lease regression tests passed.'
