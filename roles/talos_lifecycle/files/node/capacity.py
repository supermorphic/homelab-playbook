"""Conservative survivor placement and request-headroom check for node lifecycle."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


class CapacityError(ValueError):
    pass


CPU_SUFFIXES = {"n": 1 / 1_000_000, "u": 1 / 1_000, "m": 1}
BINARY_SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}
DECIMAL_SUFFIXES = {
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}


def quantity(value: str, resource: str) -> int:
    text = str(value)
    if resource == "cpu":
        for suffix, multiplier in CPU_SUFFIXES.items():
            if text.endswith(suffix):
                return int(float(text[: -len(suffix)]) * multiplier)
        return int(float(text) * 1000)
    for suffix, multiplier in BINARY_SUFFIXES.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * multiplier)
    for suffix, multiplier in DECIMAL_SUFFIXES.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * multiplier)
    return int(float(text))


def add_resources(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    result = dict(left)
    for name, value in right.items():
        result[name] = result.get(name, 0) + value
    return result


def max_resources(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {name: max(left.get(name, 0), right.get(name, 0)) for name in left | right}


def request_map(container: dict[str, Any]) -> dict[str, int]:
    requests = container.get("resources", {}).get("requests", {})
    return {name: quantity(value, name) for name, value in requests.items()}


def pod_requests(pod: dict[str, Any]) -> dict[str, int]:
    regular: dict[str, int] = {}
    for container in pod.get("spec", {}).get("containers", []):
        regular = add_resources(regular, request_map(container))
    init_max: dict[str, int] = {}
    for container in pod.get("spec", {}).get("initContainers", []):
        init_max = max_resources(init_max, request_map(container))
    total = max_resources(regular, init_max)
    overhead = pod.get("spec", {}).get("overhead", {})
    return add_resources(total, {name: quantity(value, name) for name, value in overhead.items()})


def controlled_kind(pod: dict[str, Any]) -> str:
    for owner in pod.get("metadata", {}).get("ownerReferences", []):
        if owner.get("controller") is True:
            return str(owner.get("kind", ""))
    return ""


def tolerates(taint: dict[str, Any], tolerations: list[dict[str, Any]]) -> bool:
    for toleration in tolerations:
        if toleration.get("effect") not in (None, "", taint.get("effect")):
            continue
        operator = toleration.get("operator", "Equal")
        if operator == "Exists" and toleration.get("key", "") in ("", taint.get("key")):
            return True
        if (
            operator == "Equal"
            and toleration.get("key") == taint.get("key")
            and toleration.get("value", "") == taint.get("value", "")
        ):
            return True
    return False


def expression_matches(expression: dict[str, Any], labels: dict[str, str]) -> bool:
    key = expression.get("key", "")
    operator = expression.get("operator")
    values = expression.get("values", [])
    present = key in labels
    if operator == "In":
        return present and labels[key] in values
    if operator == "NotIn":
        return not present or labels[key] not in values
    if operator == "Exists":
        return present
    if operator == "DoesNotExist":
        return not present
    if operator in ("Gt", "Lt") and present and len(values) == 1:
        try:
            current, expected = int(labels[key]), int(values[0])
        except ValueError:
            return False
        return current > expected if operator == "Gt" else current < expected
    return False


def node_affinity_matches(pod: dict[str, Any], node: dict[str, Any]) -> bool:
    affinity = pod.get("spec", {}).get("affinity", {})
    terms = (
        affinity.get("nodeAffinity", {})
        .get("requiredDuringSchedulingIgnoredDuringExecution", {})
        .get("nodeSelectorTerms", [])
    )
    if not terms:
        return True
    labels = node.get("metadata", {}).get("labels", {})
    node_name = node.get("metadata", {}).get("name", "")
    for term in terms:
        expressions = term.get("matchExpressions", [])
        fields = term.get("matchFields", [])
        if all(expression_matches(item, labels) for item in expressions) and all(
            expression_matches(item, {"metadata.name": node_name}) for item in fields
        ):
            return True
    return False


def node_selector_matches(pod: dict[str, Any], node: dict[str, Any]) -> bool:
    labels = node.get("metadata", {}).get("labels", {})
    return all(
        labels.get(key) == value
        for key, value in pod.get("spec", {}).get("nodeSelector", {}).items()
    )


def taints_allow(pod: dict[str, Any], node: dict[str, Any]) -> bool:
    tolerations = pod.get("spec", {}).get("tolerations", [])
    for taint in node.get("spec", {}).get("taints", []):
        if taint.get("effect") in ("NoSchedule", "NoExecute") and not tolerates(
            taint, tolerations
        ):
            return False
    return True


def base_eligible(pod: dict[str, Any], node: dict[str, Any]) -> bool:
    return (
        node_selector_matches(pod, node)
        and node_affinity_matches(pod, node)
        and taints_allow(pod, node)
    )


def selector_matches(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    return all(
        expression_matches(expression, labels)
        for expression in selector.get("matchExpressions", [])
    )


def term_namespaces(term: dict[str, Any], pod: dict[str, Any]) -> set[str]:
    if term.get("namespaceSelector") is not None:
        raise CapacityError("affinity namespaceSelector needs scheduler evidence")
    namespaces = term.get("namespaces", [])
    if namespaces:
        return {str(namespace) for namespace in namespaces}
    return {str(pod.get("metadata", {}).get("namespace", ""))}


def affinity_term_matches(
    term: dict[str, Any],
    pod: dict[str, Any],
    candidate: str,
    survivors: dict[str, dict[str, Any]],
    pods: dict[str, Any],
    placements: dict[int, str],
) -> bool:
    if term.get("matchLabelKeys") or term.get("mismatchLabelKeys"):
        raise CapacityError("affinity matchLabelKeys needs scheduler evidence")
    topology_key = term.get("topologyKey", "")
    candidate_domain = survivors[candidate].get("metadata", {}).get("labels", {}).get(topology_key)
    if not topology_key or candidate_domain is None:
        return False
    namespaces = term_namespaces(term, pod)
    selector = term.get("labelSelector", {})
    for existing in pods.get("items", []):
        if existing.get("metadata", {}).get("namespace", "") not in namespaces:
            continue
        if existing.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        if not selector_matches(selector, existing.get("metadata", {}).get("labels", {})):
            continue
        node_name = placements.get(id(existing), existing.get("spec", {}).get("nodeName", ""))
        if node_name not in survivors:
            continue
        domain = survivors[node_name].get("metadata", {}).get("labels", {}).get(topology_key)
        if domain == candidate_domain:
            return True
    return False


def interpod_affinity_allows(
    pod: dict[str, Any],
    candidate: str,
    survivors: dict[str, dict[str, Any]],
    pods: dict[str, Any],
    placements: dict[int, str],
) -> bool:
    affinity = pod.get("spec", {}).get("affinity", {})
    required_affinity = affinity.get("podAffinity", {}).get(
        "requiredDuringSchedulingIgnoredDuringExecution", []
    )
    if not all(
        affinity_term_matches(term, pod, candidate, survivors, pods, placements)
        for term in required_affinity
    ):
        return False
    required_anti_affinity = affinity.get("podAntiAffinity", {}).get(
        "requiredDuringSchedulingIgnoredDuringExecution", []
    )
    if any(
        affinity_term_matches(term, pod, candidate, survivors, pods, placements)
        for term in required_anti_affinity
    ):
        return False

    incoming_labels = pod.get("metadata", {}).get("labels", {})
    incoming_namespace = str(pod.get("metadata", {}).get("namespace", ""))
    for existing in pods.get("items", []):
        if existing.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        existing_node = placements.get(id(existing), existing.get("spec", {}).get("nodeName", ""))
        if existing_node not in survivors:
            continue
        terms = (
            existing.get("spec", {})
            .get("affinity", {})
            .get("podAntiAffinity", {})
            .get("requiredDuringSchedulingIgnoredDuringExecution", [])
        )
        for term in terms:
            if term.get("matchLabelKeys") or term.get("mismatchLabelKeys"):
                raise CapacityError("affinity matchLabelKeys needs scheduler evidence")
            if incoming_namespace not in term_namespaces(term, existing):
                continue
            if not selector_matches(term.get("labelSelector", {}), incoming_labels):
                continue
            topology_key = term.get("topologyKey", "")
            candidate_domain = (
                survivors[candidate].get("metadata", {}).get("labels", {}).get(topology_key)
            )
            existing_domain = (
                survivors[existing_node].get("metadata", {}).get("labels", {}).get(topology_key)
            )
            if (
                topology_key
                and candidate_domain is not None
                and candidate_domain == existing_domain
            ):
                return False
    return True


def topology_spread_allows(
    pod: dict[str, Any],
    candidate: str,
    survivors: dict[str, dict[str, Any]],
    pods: dict[str, Any],
    placements: dict[int, str],
) -> bool:
    for constraint in pod.get("spec", {}).get("topologySpreadConstraints", []):
        if constraint.get("whenUnsatisfiable") != "DoNotSchedule":
            continue
        if constraint.get("matchLabelKeys"):
            raise CapacityError("topology spread matchLabelKeys needs scheduler evidence")
        affinity_policy = constraint.get("nodeAffinityPolicy", "Honor")
        taints_policy = constraint.get("nodeTaintsPolicy", "Ignore")
        if affinity_policy not in ("Honor", "Ignore"):
            raise CapacityError("invalid topology spread nodeAffinityPolicy")
        if taints_policy not in ("Honor", "Ignore"):
            raise CapacityError("invalid topology spread nodeTaintsPolicy")
        topology_key = constraint.get("topologyKey", "")
        max_skew = constraint.get("maxSkew")
        if not topology_key or not isinstance(max_skew, int) or max_skew < 1:
            raise CapacityError("invalid hard topology spread constraint")
        eligible_nodes = [
            node
            for node in survivors.values()
            if (
                affinity_policy == "Ignore"
                or (node_selector_matches(pod, node) and node_affinity_matches(pod, node))
            )
            and (taints_policy == "Ignore" or taints_allow(pod, node))
        ]
        domains = {
            node.get("metadata", {}).get("labels", {}).get(topology_key)
            for node in eligible_nodes
            if node.get("metadata", {}).get("labels", {}).get(topology_key)
        }
        candidate_domain = (
            survivors[candidate].get("metadata", {}).get("labels", {}).get(topology_key)
        )
        if candidate_domain not in domains:
            return False
        counts = {domain: 0 for domain in domains}
        selector = constraint.get("labelSelector", {})
        namespace = pod.get("metadata", {}).get("namespace", "")
        for existing in pods.get("items", []):
            if existing.get("metadata", {}).get("namespace", "") != namespace:
                continue
            if existing.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            if not selector_matches(selector, existing.get("metadata", {}).get("labels", {})):
                continue
            node_name = placements.get(id(existing), existing.get("spec", {}).get("nodeName", ""))
            if node_name not in survivors:
                continue
            domain = survivors[node_name].get("metadata", {}).get("labels", {}).get(topology_key)
            if domain in counts:
                counts[domain] += 1
        counts[candidate_domain] += 1
        min_domains = constraint.get("minDomains", 1)
        if not isinstance(min_domains, int) or isinstance(min_domains, bool) or min_domains < 1:
            raise CapacityError("invalid hard topology spread minDomains")
        minimum = min(counts.values()) if len(domains) >= min_domains else 0
        if counts[candidate_domain] - minimum > max_skew:
            return False
    return True


def fits(requests: dict[str, int], available: dict[str, int]) -> bool:
    return all(value <= available.get(name, 0) for name, value in requests.items())


def evaluate(target: str, nodes: dict[str, Any], pods: dict[str, Any]) -> None:
    survivors = {
        node["metadata"]["name"]: node
        for node in nodes.get("items", [])
        if node.get("metadata", {}).get("name") != target
    }
    if len(survivors) != 2:
        raise CapacityError("exactly two surviving Nodes are required")
    available: dict[str, dict[str, int]] = {}
    for name, node in survivors.items():
        allocatable = node.get("status", {}).get("allocatable", {})
        available[name] = {
            resource: quantity(value, resource) for resource, value in allocatable.items()
        }
        available[name]["pods"] = available[name].get("pods", 0)

    for pod in pods.get("items", []):
        spec = pod.get("spec", {})
        node_name = spec.get("nodeName", "")
        if node_name not in survivors or pod.get("status", {}).get("phase") in (
            "Succeeded",
            "Failed",
        ):
            continue
        used = pod_requests(pod)
        used["pods"] = 1
        for resource, value in used.items():
            available[node_name][resource] = available[node_name].get(resource, 0) - value

    displaced = []
    for pod in pods.get("items", []):
        if pod.get("spec", {}).get("nodeName") != target:
            continue
        if pod.get("metadata", {}).get("annotations", {}).get("kubernetes.io/config.mirror"):
            continue
        kind = controlled_kind(pod)
        if kind == "DaemonSet":
            continue
        if not kind:
            raise CapacityError(
                f"unmanaged Pod {pod['metadata']['namespace']}/{pod['metadata']['name']}"
            )
        displaced.append(pod)

    displaced.sort(key=lambda pod: sum(pod_requests(pod).values()), reverse=True)
    placements: dict[int, str] = {}
    for pod in displaced:
        requests = pod_requests(pod)
        requests["pods"] = 1
        candidates = [
            name
            for name, node in survivors.items()
            if base_eligible(pod, node)
            and interpod_affinity_allows(pod, name, survivors, pods, placements)
            and topology_spread_allows(pod, name, survivors, pods, placements)
            and fits(requests, available[name])
        ]
        if not candidates:
            identity = f"{pod['metadata']['namespace']}/{pod['metadata']['name']}"
            raise CapacityError(f"no survivor has eligible request headroom for {identity}")
        chosen = max(
            candidates,
            key=lambda name: sum(
                available[name].get(resource, 0) - value for resource, value in requests.items()
            ),
        )
        for resource, value in requests.items():
            available[chosen][resource] -= value
        placements[id(pod)] = chosen


def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: capacity.py <target-node> <nodes-json> <pods-json>", file=sys.stderr)
        return 2
    target, nodes_path, pods_path = sys.argv[1:]
    try:
        evaluate(
            target,
            json.loads(Path(nodes_path).read_text()),
            json.loads(Path(pods_path).read_text()),
        )
    except (CapacityError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Survivor capacity check failed: {error}.", file=sys.stderr)
        return 1
    print(f"Survivor capacity check passed for workloads displaced from {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
