#!/usr/bin/env bash
# SSH 進入 GCP VM
#
# 用法:
#   bash deploy/ssh_vm.sh

set -euo pipefail

GCLOUD="$(dirname "$0")/../google-cloud-sdk/bin/gcloud"
GCP_PROJECT="${GCP_PROJECT:-storied-glazing-479907-d9}"
GCP_ZONE="${GCP_ZONE:-us-west1-b}"
VM_INSTANCE="${VM_INSTANCE:-stonk}"
VM_USER="${VM_USER:-guang_zhwa}"

exec "$GCLOUD" compute ssh "${VM_USER}@${VM_INSTANCE}" \
  --project "$GCP_PROJECT" --zone "$GCP_ZONE"
