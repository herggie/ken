#!/usr/bin/env bash
# k8s-tunnel.sh — open one SSH tunnel that forwards every NodePort service
# (plus the Kubernetes API) from the kskubemstr control-plane node to your
# local machine. Run it in WSL.
#
# Everything rides inside the single SSH connection on port 22, so the
# perimeter firewall / Zscaler only ever sees allowed SSH traffic.
#
#   ./k8s-tunnel.sh                 # discover NodePorts, forward them all
#   ./k8s-tunnel.sh -n              # dry run: print the ssh command, don't connect
#
# Credentials are NOT stored in this file. If sshpass is installed and you
# want non-interactive auth, export the password first:
#     export KSSH_PASS='...'
# Otherwise ssh will just prompt you for the password (and sudo password)
# the normal way.
#
# After it's up, browse each service at http://localhost:<nodeport>.
# Reminder: add "localhost;127.0.0.1" to your browser/OS proxy bypass list
# so Zscaler doesn't intercept your own forwarded ports.

set -uo pipefail

HOST="${KSSH_HOST:-10.157.246.195}"
USER_NAME="${KSSH_USER:-ken}"
API_LOCAL_PORT="${KSSH_API_PORT:-6443}"   # local port for the kube-apiserver forward
DRY_RUN=0

while getopts ":n" opt; do
  case $opt in
    n) DRY_RUN=1 ;;
    *) : ;;
  esac
done

# Choose how to run ssh: with sshpass if a password is exported, else plain.
SSHPASS_PREFIX=()
if [ -n "${KSSH_PASS:-}" ]; then
  if command -v sshpass >/dev/null 2>&1; then
    SSHPASS_PREFIX=(sshpass -e)   # -e reads password from $SSHPASS
    export SSHPASS="$KSSH_PASS"
  else
    echo "note: KSSH_PASS set but sshpass not installed; ssh will prompt instead."
  fi
fi

SSH_COMMON=(-o StrictHostKeyChecking=no -o ConnectTimeout=15)

echo ">> discovering NodePort services on $HOST ..."
# The remote kubectl call needs sudo. If a password is exported we feed it to
# sudo -S; otherwise sudo will prompt on the remote side.
REMOTE_SUDO='sudo'
[ -n "${KSSH_PASS:-}" ] && REMOTE_SUDO="echo \"\$KSSH_PASS_REMOTE\" | sudo -S"

JSONPATH='{range .items[?(@.spec.type=="NodePort")]}{.metadata.namespace}{" "}{.metadata.name}{" "}{range .spec.ports[*]}{.nodePort}{","}{end}{"\n"}{end}'

SVC_RAW=$(KSSH_PASS_REMOTE="${KSSH_PASS:-}" "${SSHPASS_PREFIX[@]}" ssh "${SSH_COMMON[@]}" \
  "$USER_NAME@$HOST" \
  "KSSH_PASS_REMOTE='${KSSH_PASS:-}' bash -c '${REMOTE_SUDO} kubectl get svc -A -o jsonpath=\"${JSONPATH}\"' 2>/dev/null")

if [ -z "$SVC_RAW" ]; then
  echo "!! no NodePort services returned (SSH/sudo failed, or none exist)."
  echo "   Try:  KSSH_PASS='<pw>' ./k8s-tunnel.sh    (needs sshpass)"
  echo "   or run ssh manually to confirm access first."
  exit 1
fi

# Build the -L forward list: one forward per unique nodePort -> localhost:<nodePort>.
declare -A SEEN
FORWARDS=()
MAP=()
while read -r ns name ports; do
  [ -z "${ports:-}" ] && continue
  IFS=',' read -ra plist <<< "$ports"
  for p in "${plist[@]}"; do
    [ -z "$p" ] && continue
    if [ -z "${SEEN[$p]:-}" ]; then
      SEEN[$p]=1
      FORWARDS+=(-L "$p:localhost:$p")
      MAP+=("  http://localhost:$p    ($ns/$name)")
    fi
  done
done <<< "$SVC_RAW"

# Always add the kube-apiserver so kubectl works locally too.
FORWARDS+=(-L "$API_LOCAL_PORT:localhost:6443")

echo
echo ">> will forward these services:"
printf '%s\n' "${MAP[@]}"
echo "  https://localhost:$API_LOCAL_PORT  (kube-apiserver)"
echo

# Prefer autossh (auto-reconnect) if present, else plain ssh.
if command -v autossh >/dev/null 2>&1; then
  SSH_BIN=autossh; PRE=(-M 0)
else
  SSH_BIN=ssh; PRE=()
  echo "note: install autossh for auto-reconnect (apt-get install -y autossh)"
fi

CMD=("${SSHPASS_PREFIX[@]}" "$SSH_BIN" "${PRE[@]}" -N \
     -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
     -o ExitOnForwardFailure=yes \
     "${FORWARDS[@]}" "$USER_NAME@$HOST")

if [ "$DRY_RUN" -eq 1 ]; then
  echo ">> dry run — command that would run (password redacted):"
  printf '%q ' "${CMD[@]}"; echo
  exit 0
fi

echo ">> opening tunnel (Ctrl-C to close) ..."
exec "${CMD[@]}"
