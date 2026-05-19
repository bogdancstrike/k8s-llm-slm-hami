#!/usr/bin/env bash
set -euo pipefail

HOSTS=(
  argocd.local.ro
  gitlab.local.ro
  grafana.local.ro
  open-webui.local.ro
  langfuse.local.ro
  jaeger.local.ro
  litellm.local.ro
)

for host in "${HOSTS[@]}"; do
  if ! grep -qE "^[0-9.]+[[:space:]].*(^|[[:space:]])${host}([[:space:]]|$)" /etc/hosts; then
    echo "127.0.0.1 ${host}" | sudo tee -a /etc/hosts >/dev/null
  fi
done

echo "Local hostnames configured."
