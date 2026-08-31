#!/usr/bin/env bash
# k8s-triage.sh — read-only Kubernetes control-plane triage collector.
#
# Run on the control-plane node. Collects cluster state into a single file
# you can read or hand to someone else.
#
#   ./k8s-triage.sh              # writes ./k8s-triage-<host>-<ts>.txt
#   ./k8s-triage.sh -o /tmp/x    # explicit output path
#
# Read-only: every command inspects state. Nothing is created, edited or
# deleted. Secret *values* are never dumped -- only names and types.

set -uo pipefail   # deliberately not -e: a failing probe is itself a datapoint

OUT=""
while getopts ":o:h" opt; do
  case $opt in
    o) OUT="$OPTARG" ;;
    h) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option -$OPTARG" >&2; exit 2 ;;
  esac
done

[ -n "$OUT" ] || OUT="./k8s-triage-$(hostname -s 2>/dev/null || echo host)-$(date +%Y%m%d-%H%M%S).txt"

# sudo only if we aren't already root and it exists
if [ "$(id -u)" -eq 0 ]; then SUDO=""
elif command -v sudo >/dev/null 2>&1; then SUDO="sudo"
else SUDO=""; echo "note: not root and no sudo; host-level sections may be thin" >&2
fi

section() { printf '\n\n===== %s =====\n' "$*"; }
run() {
  printf '\n--- $ %s\n' "$*"
  timeout 60 bash -c "$*" 2>&1 || printf '[command failed or timed out: exit %s]\n' "$?"
}

{
printf 'k8s triage — %s — %s\n' "$(hostname -f 2>/dev/null || hostname)" "$(date -Is)"

section "IDENTITY / VERSIONS"
run "uname -a"
run "cat /etc/os-release | head -5"
run "kubectl version -o yaml"
run "kubeadm version 2>/dev/null"
run "uptime"

section "CLUSTER REACHABILITY"
# If this fails, most sections below are meaningless -- check kubelet + etcd.
run "kubectl cluster-info"
run "kubectl get --raw='/readyz?verbose'"

section "NODES"
run "kubectl get nodes -o wide"
run "kubectl describe nodes"

section "UNHEALTHY WORKLOADS"
# Anything not Running/Completed is what you care about first.
run "kubectl get pods -A -o wide | grep -vE '[[:space:]]+(Running|Completed)[[:space:]]+' "
run "kubectl get pods -A --field-selector=status.phase=Pending -o wide"
run "kubectl get deploy,sts,ds -A | grep -vE '([0-9]+)/\\1'"

section "CONTROL PLANE PODS"
run "kubectl -n kube-system get pods -o wide"
run "kubectl -n kube-system get pods -o wide | grep -vE '[[:space:]]+Running[[:space:]]+'"

section "RECENT EVENTS (warnings first)"
run "kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp | tail -50"
run "kubectl get events -A --sort-by=.lastTimestamp | tail -60"

section "KUBELET"
run "$SUDO systemctl status kubelet --no-pager -l | head -40"
run "$SUDO journalctl -u kubelet --no-pager -n 200 --since '2 hours ago'"

section "CONTAINER RUNTIME"
run "$SUDO systemctl status containerd docker crio --no-pager -l 2>/dev/null | head -40"
run "$SUDO crictl info 2>/dev/null | head -40"
run "$SUDO crictl ps -a 2>/dev/null | head -40"

section "ETCD"
run "kubectl -n kube-system get pods -l component=etcd -o wide"
run "$SUDO journalctl -u etcd --no-pager -n 80 --since '2 hours ago' 2>/dev/null"
run "kubectl -n kube-system logs -l component=etcd --tail=80 2>/dev/null"

section "NETWORKING / CNI"
run "kubectl -n kube-system get pods -o wide | grep -iE 'calico|flannel|cilium|weave|kube-proxy|canal'"
run "ls -1 /etc/cni/net.d/ 2>/dev/null"
run "ip -brief addr 2>/dev/null || ifconfig -a 2>/dev/null | head -40"
run "ip route 2>/dev/null"

section "DNS"
run "kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide"
run "kubectl -n kube-system logs -l k8s-app=kube-dns --tail=60 2>/dev/null"
run "kubectl -n kube-system get svc kube-dns"

section "CERTIFICATES (expiry is a classic silent killer)"
run "$SUDO kubeadm certs check-expiration 2>/dev/null"

section "STORAGE / PV"
run "kubectl get pv,pvc -A"
run "kubectl get sc"

section "HOST RESOURCES"
run "df -h"
run "free -h"
run "$SUDO dmesg -T 2>/dev/null | grep -iE 'oom|killed process|no space' | tail -30"

section "API RESOURCES SUMMARY"
run "kubectl get ns"
run "kubectl top nodes 2>/dev/null"
run "kubectl top pods -A --sort-by=memory 2>/dev/null | head -25"

section "SECRETS (names/types only — values intentionally excluded)"
run "kubectl get secrets -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,TYPE:.type"

printf '\n\n===== END =====\n'
} > "$OUT" 2>&1

echo "wrote: $OUT"
echo "size:  $(wc -l < "$OUT") lines"
echo
echo "Review it before sharing — cluster-info and events can contain hostnames"
echo "and endpoint addresses you may want to redact."
