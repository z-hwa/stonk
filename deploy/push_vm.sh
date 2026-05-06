#!/usr/bin/env bash
# 一鍵推送到 GCP VM 並重啟服務
#
# 用法:
#   bash deploy/push_vm.sh           # git push + VM pull + restart
#   bash deploy/push_vm.sh --env     # 同上，額外把 .env 同步到 VM
#
# 前置條件:
#   - 已執行 gcloud auth login
#   - VM 上 /opt/stonk 的 git remote 與本地相同
#   - VM 上已設定 NOPASSWD sudo for systemctl restart stonk

set -euo pipefail

GCLOUD="$(dirname "$0")/../google-cloud-sdk/bin/gcloud"
GCP_PROJECT="${GCP_PROJECT:-storied-glazing-479907-d9}"
GCP_ZONE="${GCP_ZONE:-us-west1-b}"
VM_INSTANCE="${VM_INSTANCE:-stonk}"
VM_USER="${VM_USER:-guang_zhwa}"
VM_DIR="${VM_DIR:-/opt/stonk}"
SYNC_ENV=false

for arg in "$@"; do
  [[ "$arg" == "--env" ]] && SYNC_ENV=true
done

echo "▶ git push..."
git push

if $SYNC_ENV; then
  echo "▶ 同步 .env 到 VM..."
  "$GCLOUD" compute scp .env "${VM_INSTANCE}:${VM_DIR}/.env" \
    --project "$GCP_PROJECT" --zone "$GCP_ZONE"
fi

echo "▶ VM: git pull + restart stonk..."
"$GCLOUD" compute ssh "${VM_USER}@${VM_INSTANCE}" \
  --project "$GCP_PROJECT" --zone "$GCP_ZONE" \
  --command "cd ${VM_DIR} && git pull && sudo systemctl restart stonk && echo '✅ 服務已重啟' && systemctl status stonk --no-pager -l | tail -5"
