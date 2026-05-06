#!/usr/bin/env bash
# 一鍵推送到 GCP VM 並重啟服務
#
# 用法:
#   bash deploy/push_vm.sh           # git push + VM pull + restart
#   bash deploy/push_vm.sh --env     # 同上，額外把 .env 同步到 VM
#
# 前置條件:
#   - ~/.ssh/config 已設定 Host stonk (guang_zhwa@<ip>)
#   - VM 上 /opt/stonk 的 git remote 與本地相同
#   - VM 上 stonk user 有 NOPASSWD sudo for systemctl restart stonk
#     (設定方式見腳本底部說明)

set -euo pipefail

VM_HOST="${VM_HOST:-stonk}"
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
  scp .env "${VM_USER}@${VM_HOST}:${VM_DIR}/.env"
fi

echo "▶ VM: git pull + restart stonk..."
ssh "${VM_USER}@${VM_HOST}" bash <<EOF
  set -e
  cd ${VM_DIR}
  git pull
  sudo systemctl restart stonk
  echo "✅ 服務已重啟"
  systemctl status stonk --no-pager -l | tail -5
EOF
