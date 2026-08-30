# Kube-VIP K3s Setup

> This K3s automation is retained but frozen. It is not an active deployment
> target. CI performs static validation only and never executes these playbooks.

## Overview

In this cluster, **Kube-VIP** provides a high-availability control-plane endpoint for the K3s API. It operates in ARP mode to advertise a shared virtual IP (VIP) across control-plane nodes, ensuring seamless failover without external load balancers.

## Components

### Kube-VIP: Control Plane High-Availability

- Deployed as a **DaemonSet** on all control-plane nodes.
- Advertises the virtual IP defined by `{{ k3s_registration_address }}`, which serves as the stable entrypoint for the K3s API server.
- Runs in **ARP mode** for layer-2 announcements of the VIP.
- Uses Kubernetes **leader election** to determine which node advertises the VIP.
- If the current leader node fails, another control-plane node automatically takes over and announces the same VIP.
- **Does not handle Kubernetes `Service` resources** or act as a Service LoadBalancer.

Together, these components deliver a resilient transition from single-namespace bootstrap networking to full bare-metal load balancing, without conflicting IP ownership or downtime.

## Deployment Guide

### Generating the Kube-VIP DaemonSet

To set up Kube-VIP for HA control-plane failover, generate the DaemonSet using the official Kube-VIP CLI.

### Prerequisites

Set the following environment variables before generating the daemonset:

```bash
export VIP=192.168.90.98
export INTERFACE=eth0
export KVVERSION=$(curl -sL https://api.github.com/repos/kube-vip/kube-vip/releases | jq -r ".[0].name")
```

### Aliases for Kube-VIP

You can create aliases for easy access to the kube-vip commands:

```bash
alias kube-vip="ctr image pull ghcr.io/kube-vip/kube-vip:$KVVERSION; ctr run --rm --net-host ghcr.io/kube-vip/kube-vip:$KVVERSION vip /kube-vip"
alias kube-vip="podman run --network host --rm ghcr.io/kube-vip/kube-vip:$KVVERSION"
```

### Generate the Daemonset

Run the following command to generate the kube-vip daemonset:

```bash
kube-vip manifest daemonset \
  --interface $INTERFACE \
  --address $VIP \
  --taint \
  --inCluster \
  --controlplane \
  --arp \
  --leaderElection \
  --leaseDuration 15 \
  --leaseRenewDuration 10 \
  --leaseRetry 2
```

This command generates a DaemonSet that:

- Runs on tainted (control-plane) nodes only.
- Advertises the VIP on the active leader.
- Provides automatic failover without Service LB mode.

Here’s the updated section for your `roles/kube_vip/templates/30-kube-vip-daemonset.yaml` file. This includes the necessary substitutions and post-editing requirements for the Ansible variables.

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  labels:
    app.kubernetes.io/name: kube-vip-ds
    app.kubernetes.io/version: v1.0.1
  name: kube-vip-ds
  namespace: kube-system
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: kube-vip-ds
  template:
    metadata:
      labels:
        app.kubernetes.io/name: kube-vip-ds
        app.kubernetes.io/version: v1.0.1
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: node-role.kubernetes.io/master
                operator: Exists
            - matchExpressions:
              - key: node-role.kubernetes.io/control-plane
                operator: Exists
      containers:
      - args:
        - manager
        env:
        - name: vip_arp
          value: "true"
        - name: port
          value: "6443"
        - name: vip_nodename
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
        - name: vip_interface
          value: eth0
        - name: vip_subnet
          value: "32"
        - name: dns_mode
          value: first
        - name: cp_enable
          value: "true"
        - name: cp_namespace
          value: kube-system
        - name: vip_leaderelection
          value: "true"
        - name: vip_leasename
          value: plndr-cp-lock
        - name: vip_leaseduration
          value: "15"
        - name: vip_renewdeadline
          value: "10"
        - name: vip_retryperiod
          value: "2"
        - name: address
          value: 192.168.90.98
        - name: prometheus_server
          value: :2112
        image: ghcr.io/kube-vip/kube-vip:v1.0.1
        imagePullPolicy: IfNotPresent
        name: kube-vip
        resources: {}
        securityContext:
          capabilities:
            add:
            - NET_ADMIN
            - NET_RAW
            drop:
            - ALL
      hostNetwork: true
      serviceAccountName: kube-vip
      tolerations:
      - effect: NoSchedule
        operator: Exists
      - effect: NoExecute
        operator: Exists
  updateStrategy: {}
```

### Post-Editing Requirements

After editing `30-kube-vip-daemonset.yaml`, ensure to perform the following substitutions in your Ansible variables:

1. **`kube_vip_version`**: Make sure this variable is defined in your Ansible variables and matches the desired version of kube-vip you're deploying.
2. **`k3s_registration_address`**: Define this variable in your playbook, providing the address that kube-vip will use as its service registration address.
3. **`vip_interface`**: Confirm that the value `eth0` is correct for your networking setup or modify it as necessary for your environment.

These substitutions will ensure that the kube-vip daemonset functions correctly with your Ansible deployment.

### Included Manifests

The following manifests are installed during cluster bootstrap:

```yaml
k3s_server_manifests_templates:
  - "{{ playbook_dir }}/../../roles/kube_vip/templates/30-kube-vip-daemonset.yaml"

k3s_server_manifests_urls:
  - url: "https://kube-vip.io/manifests/rbac.yaml"
    filename: 20-kube-vip-rbac.yaml
```

### Verification

- Confirm the DaemonSet is running on all control-plane nodes:

  ```bash
  kubectl -n kube-system get ds kube-vip-ds -o wide
  ```

- Check which node holds the control-plane lease:

  ```bash
  kubectl -n kube-system get lease plndr-cp-lock -o yaml | grep holderIdentity
  ```

- Verify the VIP is assigned on the leader node:

  ```bash
  ssh <leader-node> "ip -brief addr | grep $VIP"
  ```

- Test failover by cordoning and draining the leader:

  ```bash
  kubectl drain <leader-node> --ignore-daemonsets --delete-emptydir-data
  ```

- Confirm the VIP migrates to another node and the API remains reachable.
