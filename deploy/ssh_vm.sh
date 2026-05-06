#!/usr/bin/env bash
# SSH 進入 GCP VM
#
# 用法:
#   bash deploy/ssh_vm.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "${SCRIPT_DIR}/../.env" ] && set -a && source "${SCRIPT_DIR}/../.env" && set +a

GCLOUD="${SCRIPT_DIR}/../google-cloud-sdk/bin/gcloud"
GCP_PROJECT="${GCP_PROJECT:?請在 .env 設定 GCP_PROJECT}"
GCP_ZONE="${GCP_ZONE:?請在 .env 設定 GCP_ZONE}"
VM_INSTANCE="${VM_INSTANCE:?請在 .env 設定 VM_INSTANCE}"
VM_USER="${VM_USER:-guang_zhwa}"

exec "$GCLOUD" compute ssh "${VM_USER}@${VM_INSTANCE}" \
  --project "$GCP_PROJECT" --zone "$GCP_ZONE"
