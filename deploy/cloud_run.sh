#!/usr/bin/env bash
# 一鍵部署 positions Web UI 到 Cloud Run + 建立共用 GCS bucket
#
# 用法:
#   1. 改下面 CONFIG 區
#   2. gcloud auth login && gcloud config set project $PROJECT
#   3. bash deploy/cloud_run.sh
#
# 完成後會印出 Web URL,首次開啟記得帶 ?token=...

set -euo pipefail

# === CONFIG (改這裡) ===
PROJECT="${GCP_PROJECT:?請設定 GCP_PROJECT}"
REGION="${GCP_REGION:-asia-east1}"          # 台灣最近: asia-east1
SERVICE="${SERVICE:-stonk-positions}"
BUCKET="${BUCKET:-${PROJECT}-stonk-positions}"
TOKEN="${POSITIONS_TOKEN:?請設定 POSITIONS_TOKEN (e.g. \$(openssl rand -hex 24))}"
# =======================

echo "▶ 啟用必要 API..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  --project "$PROJECT"

echo "▶ 建立 GCS bucket (若不存在)..."
gsutil ls -b "gs://${BUCKET}" >/dev/null 2>&1 || \
  gsutil mb -p "$PROJECT" -l "$REGION" -b on "gs://${BUCKET}"

echo "▶ 建立 service account..."
SA_NAME="stonk-positions-sa"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name "Stonk Positions Web UI" \
    --project "$PROJECT"

echo "▶ 授權 SA 讀寫 bucket..."
gsutil iam ch \
  "serviceAccount:${SA_EMAIL}:objectAdmin" \
  "gs://${BUCKET}"

echo "▶ Build & deploy to Cloud Run..."
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --allow-unauthenticated \
  --memory 256Mi \
  --cpu 1 \
  --max-instances 2 \
  --min-instances 0 \
  --concurrency 20 \
  --timeout 30 \
  --set-env-vars "POSITIONS_BACKEND=gcs,POSITIONS_GCS_BUCKET=${BUCKET},POSITIONS_TOKEN=${TOKEN}"

URL=$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT" --region "$REGION" --format='value(status.url)')

echo
echo "✅ 部署完成"
echo "   Web UI:  ${URL}/?token=${TOKEN}"
echo "   Bucket:  gs://${BUCKET}/_positions.json"
echo "   SA:      ${SA_EMAIL}"
echo
echo "▼ VM 端 .env 加上:"
echo "   POSITIONS_BACKEND=gcs"
echo "   POSITIONS_GCS_BUCKET=${BUCKET}"
echo "   GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json"
echo
echo "▼ 產生 VM 用的 SA key:"
echo "   gcloud iam service-accounts keys create sa-key.json \\"
echo "     --iam-account=${SA_EMAIL} --project=${PROJECT}"
