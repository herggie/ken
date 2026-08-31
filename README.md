# ken

Operational tooling for the `ken` Kubernetes lab cluster.

## scripts/k8s-triage.sh

Read-only triage collector. Run it on the control-plane node; it writes a
single timestamped text file with everything needed to diagnose a sick
cluster.

```bash
./scripts/k8s-triage.sh              # -> ./k8s-triage-<host>-<ts>.txt
./scripts/k8s-triage.sh -o /tmp/out.txt
```

What it collects: versions, API reachability (`/readyz?verbose`), node
status and `describe`, any pod not Running/Completed, control-plane pods,
warning events, kubelet and container-runtime state and logs, etcd, CNI and
kube-proxy, CoreDNS, **certificate expiry** (`kubeadm certs check-expiration`
— a common silent cause of a cluster that "just stopped working"), PV/PVC,
and host disk/memory/OOM history.

Safety notes:

- Every command is an inspection. Nothing is created, modified or deleted.
- Secret **values** are never emitted — only namespace, name and type.
- Each probe is wrapped in `timeout 60`, and a failing probe is recorded
  rather than aborting the run (a hung `kubectl` is itself a finding).
- Output can still contain hostnames and API endpoints. Skim before sharing.
