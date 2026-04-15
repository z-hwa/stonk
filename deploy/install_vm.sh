#!/usr/bin/env bash
# 在裸 Linux VM 上一鍵安裝 stonk 排程服務
# 用法 (Ubuntu/Debian, 以 root 或 sudo 執行):
#   bash deploy/install_vm.sh https://github.com/<you>/stonk.git

set -euo pipefail

REPO_URL="${1:-${REPO_URL:?用法: bash install_vm.sh <git-url>}}"
INSTALL_DIR="${INSTALL_DIR:-/opt/stonk}"
SERVICE_USER="${SERVICE_USER:-stonk}"
CONDA_DIR="${CONDA_DIR:-/opt/conda}"
ENV_NAME="${ENV_NAME:-quant}"

echo "▶ 安裝系統套件..."
apt-get update -qq
apt-get install -y -qq git curl ca-certificates

echo "▶ 建立服務使用者..."
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$SERVICE_USER"

echo "▶ 安裝 Miniconda (若未安裝)..."
if [ ! -d "$CONDA_DIR" ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64)  MC=Miniconda3-latest-Linux-x86_64.sh ;;
    aarch64) MC=Miniconda3-latest-Linux-aarch64.sh ;;
    *) echo "不支援的架構: $ARCH"; exit 1 ;;
  esac
  curl -fsSL "https://repo.anaconda.com/miniconda/$MC" -o /tmp/mc.sh
  bash /tmp/mc.sh -b -p "$CONDA_DIR"
  rm /tmp/mc.sh
fi

echo "▶ Clone repo..."
if [ ! -d "$INSTALL_DIR/.git" ]; then
  git clone "$REPO_URL" "$INSTALL_DIR"
else
  git -C "$INSTALL_DIR" pull
fi

echo "▶ 建立 conda env + 安裝依賴..."
if ! "$CONDA_DIR/bin/conda" env list | grep -q "^${ENV_NAME} "; then
  "$CONDA_DIR/bin/conda" create -n "$ENV_NAME" python=3.11 -y
fi
"$CONDA_DIR/envs/$ENV_NAME/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "▶ 設定權限..."
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

if [ ! -f "$INSTALL_DIR/.env" ]; then
  echo "⚠ 請先建立 $INSTALL_DIR/.env (參考 .env.example),再執行下一步"
  exit 1
fi

echo "▶ 安裝 systemd unit..."
install -m 644 "$INSTALL_DIR/deploy/stonk.service" /etc/systemd/system/stonk.service
systemctl daemon-reload
systemctl enable stonk.service
systemctl restart stonk.service

echo
echo "✅ 安裝完成"
echo "   狀態: systemctl status stonk"
echo "   日誌: journalctl -u stonk -f"
