"""Validate explicit rootless identities against effective host allocations."""

import re

FIELDS = {
    "name",
    "uid",
    "gid",
    "subuid_start",
    "subuid_count",
    "subgid_start",
    "subgid_count",
}
MAX_ID = 4294967294


def _number(value, minimum=1):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= MAX_ID
    ):
        raise ValueError("identity fields must be bounded positive integers")
    return value


def _database(text, fields):
    entries = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != fields:
            raise ValueError("malformed host identity database")
        entries.append(parts)
    return entries


def _overlaps(start, count, other_start, other_count):
    return start < other_start + other_count and other_start < start + count


def file_subid_provider(text):
    """Accept only the default file provider or one explicit files entry."""
    providers = []
    for line in text.splitlines():
        name, separator, sources = line.partition("#")[0].partition(":")
        if separator and name.strip() == "subid":
            providers.append(sources.split())
    return providers in ([], [["files"]])


def validate_accounts(
    accounts, passwd, group, subuid, subgid, require_present=False, pending=None
):
    """Return declarations unchanged, or reject unsafe allocation/migration.

    pending contains only accounts being created by the current play. It permits
    their intermediate group/account state, never changes an existing mapping.
    """
    if not isinstance(accounts, list):
        raise ValueError("podman_foundation_accounts must be a list")
    pending = pending or []
    users = _database(passwd, 7)
    groups = _database(group, 4)
    for account in accounts:
        if not isinstance(account, dict) or set(account) != FIELDS:
            raise ValueError("account declaration has missing or unexpected fields")
        name = account["name"]
        if not isinstance(name, str) or not re.fullmatch(
            r"(?:svc|ci)-[a-z][a-z0-9-]{0,25}", name
        ):
            raise ValueError("service account name must start with svc- or ci-")
        for key in FIELDS - {"name"}:
            _number(account[key], 65536 if key.endswith("_count") else 1)
        for kind in ("uid", "gid"):
            if account[f"sub{kind}_start"] + account[f"sub{kind}_count"] - 1 > MAX_ID:
                raise ValueError("subordinate range exceeds valid identity bounds")
    for key in ("name", "uid", "gid"):
        values = [account[key] for account in accounts]
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate declared {key}")

    for kind, entries, mapping_text in [
        ("uid", users, subuid),
        ("gid", groups, subgid),
    ]:
        mappings = []
        for owner, start, count in _database(mapping_text, 3):
            try:
                start, count = int(start), int(count)
            except ValueError as error:
                raise ValueError("malformed subordinate allocation") from error
            _number(start)
            _number(count)
            if start + count - 1 > MAX_ID:
                raise ValueError("malformed subordinate allocation bounds")
            mappings.append((owner, start, count))
        ordered_mappings = sorted(mappings, key=lambda item: item[1])
        for left, right in zip(ordered_mappings, ordered_mappings[1:]):
            if _overlaps(left[1], left[2], right[1], right[2]):
                raise ValueError(f"existing subordinate {kind} ranges overlap")
        real_ids = [int(entry[2]) for entry in entries] + [a[kind] for a in accounts]
        if kind == "gid":
            real_ids.extend(int(user[3]) for user in users)
        for _, start, count in mappings:
            if any(start <= identifier < start + count for identifier in real_ids):
                raise ValueError(
                    f"existing subordinate {kind} range overlaps a host identity"
                )
        for index, account in enumerate(accounts):
            name = account["name"]
            start, count = account[f"sub{kind}_start"], account[f"sub{kind}_count"]
            for other in accounts[index + 1 :]:
                if _overlaps(
                    start, count, other[f"sub{kind}_start"], other[f"sub{kind}_count"]
                ):
                    raise ValueError(f"declared subordinate {kind} ranges overlap")
            if any(start <= identifier < start + count for identifier in real_ids):
                raise ValueError(f"subordinate {kind} range overlaps a host identity")
            own = []
            for owner, allocated_start, allocated_count in mappings:
                # Both files name the USER, even the subordinate group file.
                is_own = owner == name or (
                    owner.isdecimal() and int(owner) == account["uid"]
                )
                if is_own:
                    own.append((allocated_start, allocated_count))
                elif _overlaps(start, count, allocated_start, allocated_count):
                    raise ValueError(
                        f"subordinate {kind} range overlaps an existing allocation"
                    )
                if allocated_start <= account[kind] < allocated_start + allocated_count:
                    raise ValueError(
                        f"primary {kind} overlaps a subordinate allocation"
                    )
            exists = any(user[0] == name for user in users)
            if own and own != [(start, count)]:
                raise ValueError(
                    f"{name}: subordinate {kind} migration requires operator review"
                )
            if not own and exists and name not in pending:
                raise ValueError(
                    f"{name}: missing subordinate {kind} mapping requires migration review"
                )
            if own and not exists and name not in pending:
                raise ValueError(
                    f"{name}: orphan subordinate allocation requires migration review"
                )

    for account in accounts:
        name = account["name"]
        matches = [
            user for user in users if user[0] == name or int(user[2]) == account["uid"]
        ]
        primary = [
            entry
            for entry in groups
            if entry[0] == name or int(entry[2]) == account["gid"]
        ]
        if matches:
            if len(matches) != 1:
                raise ValueError(f"{name}: ambiguous account requires migration review")
            user = matches[0]
            if (
                user[0] != name
                or int(user[2]) != account["uid"]
                or int(user[3]) != account["gid"]
                or user[5] != f"/var/lib/{name}"
                or user[6] not in ["/usr/sbin/nologin", "/sbin/nologin"]
            ):
                raise ValueError(f"{name}: existing identity requires migration review")
        elif require_present:
            raise ValueError(f"{name}: declared account is missing")
        if primary:
            if (
                len(primary) != 1
                or primary[0][0] != name
                or int(primary[0][2]) != account["gid"]
                or primary[0][3]
                or (not matches and name not in pending)
            ):
                raise ValueError(
                    f"{name}: private group conflict requires migration review"
                )
        elif matches or require_present:
            raise ValueError(f"{name}: private group is missing")
        if any(name in entry[3].split(",") for entry in groups):
            raise ValueError(
                f"{name}: supplementary group membership requires migration review"
            )
        if any(user[0] != name and int(user[3]) == account["gid"] for user in users):
            raise ValueError(f"{name}: primary group is shared with another account")
    return accounts


def validate_reservations(accounts, reservation):
    """Keep disposable private-image identities out of host service accounts."""
    if (not isinstance(reservation, dict) or set(reservation) != {'schema', 'accounts'}
            or reservation['schema'] != 1 or isinstance(reservation['schema'], bool)
            or not isinstance(reservation['accounts'], list) or len(reservation['accounts']) > 6):
        raise ValueError('unsupported runner identity reservation')
    reserved = reservation['accounts']
    # The same independent cross-account oracle checks declarations and all
    # primary/subordinate intersections, including exact-name adoption.
    validate_accounts(accounts + reserved, '', '', '', '')
    return accounts


class FilterModule:
    def filters(self):
        return {
            "podman_foundation_validate_accounts": validate_accounts,
            "podman_foundation_file_subid_provider": file_subid_provider,
            "podman_foundation_validate_reservations": validate_reservations,
        }
