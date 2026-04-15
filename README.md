# Stonk — 美股多引擎監控系統

## 快速開始

```bash
conda activate quant
python main.py
```

排程 (UTC+8)：
- **05:30** 週一至五 — 收盤後掃描
- **21:00** 週一至五 — 盤前時機掃描 + 獲利回收
- **週日 08:00** — 長期 (年尺度) 掃描
- 每 6 小時 — 心跳通知

---

## 引擎架構

```
main.py (排程器)
├── TradeTimingEngine   — 日/週尺度買賣時機 (RSI, MACD, BB, ATR)
├── ProfitTakingEngine  — 獲利回收 + 重新入場
├── LongTermEngine      — 年尺度買賣 (8 項成長指標 + 大盤 + VIX)
└── BacktestEngine      — 回測框架 (3 年, 週頻)

data_manager.py         — 增量快取 OHLCV (parquet)
value_engine.py         — 價值選股 (PE, 市值, 位階)
engine.py               — RSI 超賣掃描
expand_db.py            — 從 NASDAQ Trader + S&P 指數擴充 watchlist

positions_store.py      — 持倉儲存抽象 (local 檔案 / GCS)
positions_server.py     — FastAPI Web UI (Cloud Run)
Dockerfile              — Cloud Run image
deploy/                 — 部署腳本 (cloud_run.sh, install_vm.sh, stonk.service)
```

---

## 分數上限與門檻

### LongTermEngine (年尺度)

**成長評分 (Growth Score) — 滿分 108**

8 項指標，Fibonacci 黃金比例加權 (前 2 項佔 63%):

| # | 指標 | 權重 | 分數範圍 | 最大貢獻 |
|---|------|------|----------|----------|
| 1 | 收益修正 | ×21 | -1 ~ +2 | +42 |
| 2 | 獲利驚喜 | ×13 | -1 ~ +2 | +26 |
| 3 | 營收成長 | ×8 | -1 ~ +2 | +16 |
| 4 | 營業利益率 | ×5 | -1 ~ +2 | +10 |
| 5 | 現金流量 | ×3 | -1 ~ +2 | +6 |
| 6 | 獲利 | ×2 | -1 ~ +2 | +4 |
| 7 | 獲利動能 | ×1 | -1 ~ +2 | +2 |
| 8 | ROE | ×1 | -1 ~ +2 | +2 |

**買入訊號 — 滿分 9**

| 訊號 | 分數 | 觸發條件 |
|------|------|----------|
| VIX 恐慌 | 2 | VIX > 25 且 < 35 |
| 回檔機會 | 2 | 距 52 週高點 ≥ 20% |
| 長期多頭 | 1 | 價格 > MA250 |
| 基本面強勁/良好/尚可 | 4/3/1 | Growth Score ≥ 65%/40%/20% |

門檻: **BUY ≥ 4** | **STRONG BUY ≥ 7**

**賣出訊號 — 滿分 ~20**

| 層級 | 訊號 | 分數 | 說明 |
|------|------|------|------|
| Tier 1 | 長期趨勢反轉 | 0-5 | 跌破MA250(+2) + 長期死叉(+2) + 回檔>30%(+1) |
| | 近期急跌 | 0-2 | 5 日跌 ≥ 8% |
| | 中期崩跌 | 0-2 | 20 日跌 ≥ 15% |
| | 跌破 MA50 | 0-1 | 連續 2 天 |
| Tier 2 | 基本面惡化/疲弱 | 0-4 | Growth raw score ≤ -15% / ≤ 0 |
| | 成長減速 | 0-2 | 營收 + EPS 雙減速 |
| Tier 3 | 技術基本面雙殺 | 0-3 | 技術破壞 AND 基本面疲弱 |
| Tier 4 | 大盤走弱 | 0-1 | S&P < MA200 |
| | 極度恐慌 | 0-1 | VIX > 35 |
| | 過熱風險 | 0-2 | 近高點 + VIX 鬆懈 |

門檻: **SELL ≥ 8** | **STRONG SELL ≥ 9**

設計哲學: 單純技術破壞可能是買點，需技術 + 基本面雙殺才觸發 STRONG SELL。

---

### TradeTimingEngine (日/週尺度)

**買入 — 滿分 8**

| 訊號 | 分數 | 條件 |
|------|------|------|
| RSI 超賣 | 2 | RSI(14) < 30 |
| MACD 金叉 | 2 | MACD 上穿信號線 (零軸下) |
| BB 下軌 | 1 | 收盤 ≤ 布林下軌 |
| MA 支撐反彈 | 2 | 價格 > MA200 且近 MA50 |
| 放量陽線 | 1 | 量 > 1.5x 均量 + 收 > 開 |

門檻: **BUY ≥ 3** | **STRONG BUY ≥ 5**

**賣出 — 滿分 10**

| 訊號 | 分數 | 條件 |
|------|------|------|
| ATR 止損 | 3 | 收盤 < 20日高 - 2×ATR |
| RSI 超買 | 1 | RSI > 70 |
| MACD 死叉 | 2 | MACD 下穿信號線 (零軸上) |
| MA50 跌破 | 2 | 連續 2 天 < MA50 |
| 停利達標 | 2 | 漲幅 ≥ 30% |

門檻: **SELL ≥ 3** | **STRONG SELL ≥ 6**

---

### ProfitTakingEngine (獲利回收)

**獲利了結 — 滿分 11** (僅對已獲利持倉評估)

| 訊號 | 分數 | 條件 |
|------|------|------|
| ATR 停利 | 3 | 收盤 < 近期高 - 2×ATR |
| RSI 過熱 | 2 | RSI > 75 |
| 量價背離 | 2 | 價格新高但量萎縮 |
| 均線乖離 | 2 | 偏離 MA50 > 15% |
| BB 上軌回落 | 1 | 前日突破上軌、今日回落 |
| 漲幅達標 | 1 | 未實現獲利 ≥ 30% |

門檻: **獲利了結 ≥ 4**

**重新入場 — 滿分 8**

| 訊號 | 分數 | 條件 |
|------|------|------|
| MA50 支撐 | 2 | 價格回到 MA50 ±2% |
| RSI 冷卻 | 2 | RSI 降到 40-55 |
| BB 中軌回踩 | 1 | 價格回到 BB 中軌附近 |
| 量縮企穩 | 1 | 波幅收窄 + 量縮 |
| 長期多頭 | 2 | MA50 > MA250 |

門檻: **重新入場 ≥ 3**

---

### BacktestEngine (回測)

```bash
# 執行 3 年回測
conda run -n quant python backtest_engine.py
```

- 週頻再平衡 (W-MON)
- 整合 LongTermEngine + ProfitTakingEngine
- 初始資金 $100,000，等權配置，最多 10 檔
- 交易成本 0.1%/筆
- 基準: SPY 買進持有
- 輸出: logs/backtest_*.log + trades CSV + equity curve CSV

限制:
1. 存活偏差 (用當前 watchlist)
2. 收益修正/獲利驚喜無歷史 → 回測停用 (Growth max 108 → 40)
3. 基本面用年度近似
4. 無滑價

---

## 設定 (.env)

```env
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

# 價值選股
MKT_CAP_MIN=10000000000
PE_MAX=30
POSITION_MAX=0.4
FUND_CACHE_DAYS=7

# 交易時機
TIMING_WATCHLIST=TSM,AVGO,HOOD,UBER,ONDS,CRWV,PLTR,CCL,ARQT,CRDO,NVDA,TWLO,CXDO
TIMING_RSI_OVERSOLD=30
TIMING_RSI_OVERBOUGHT=70
TIMING_ATR_STOP_MULT=2.0
TIMING_TAKE_PROFIT_PCT=0.30
TIMING_BUY_NOTIFY_MIN=3
TIMING_SELL_NOTIFY_MIN=3

# 年尺度
LT_WATCHLIST=                          # 空 = 沿用 TIMING_WATCHLIST
LT_VIX_PANIC=25
LT_VIX_EXTREME=35
LT_VIX_COMPLACENT=15
LT_DRAWDOWN_BUY_PCT=0.20
LT_GROWTH_MIN=0.10
LT_QUARTERLY_CACHE_DAYS=14
LT_BUY_NOTIFY_MIN=4
LT_SELL_NOTIFY_MIN=8

# 獲利回收
PT_ATR_TRAIL_MULT=2.0
PT_RSI_OVERBOUGHT=75
PT_MA_DEVIATION_PCT=0.15
PT_PROFIT_TARGET_PCT=0.30
PT_SELL_NOTIFY_MIN=4
PT_REENTRY_NOTIFY_MIN=3
```

---

## 資料庫

```bash
# 進入 SQLite
sqlite3 dataset/stocks.db

# 查看清單
SELECT * FROM watchlist;

# 新增
INSERT INTO watchlist (symbol) VALUES ('GOOGL');

# 刪除
DELETE FROM watchlist WHERE symbol = 'TSLA';

# 擴充 (NASDAQ + NYSE + S&P)
conda run -n quant python dataset/expand_db.py
```

---

## 快取結構

```
cache/
├── *.parquet              # 個股 2 年 OHLCV (日常用)
├── _fundamentals.json     # PE/市值快取 (7 天有效)
├── _quarterly.json        # 季度基本面 (14 天有效)
├── _positions.json        # 手動持倉記錄 (獲利回收用,線上用 GCS 取代)
└── backtest/
    ├── *.parquet          # 5 年 OHLCV (回測專用)
    └── _annual.json       # 年度基本面 (回測用)
```

---

## 部署 (Cloud Run + VM)

混合架構,共用 GCS 上的 `_positions.json`,費用在免費層內 **$0/月**:

```
┌──────────────────┐     讀/寫     ┌────────────────────┐
│  VM (always-on)  │ ───────────▶  │  GCS bucket        │
│  main.py 排程    │               │  _positions.json   │
└──────────────────┘               └────────────────────┘
                                            ▲
                                            │ 讀/寫
                                   ┌────────┴───────────┐
                                   │  Cloud Run         │
                                   │  positions_server  │ ◀── 手機/瀏覽器
                                   │  (scale to zero)   │
                                   └────────────────────┘
```

VM 選擇(都能跑):
- **Oracle Cloud Always Free** (x86 1 vCPU/1GB,永久免費)
- **GCP e2-micro** (1GB,僅 us-west/central/east1 免費)
- **Hetzner CAX11** (~$3.60/月,2 ARM core/4GB)

### 1. 部署 Web UI 到 Cloud Run

```bash
gcloud auth login
gcloud auth login --no-launch-browser

export GCP_PROJECT=your-project-id
export POSITIONS_TOKEN=$(openssl rand -hex 24)
bash deploy/cloud_run.sh
```

腳本會自動:啟用 API → 建 GCS bucket → 建 service account → 授權 → build & deploy。

完成後印出 `https://stonk-positions-xxx.run.app/?token=...`,直接收藏這個網址。

### 2. 部署排程器到 VM

```bash
# 在你的 VM 上 (Ubuntu/Debian)
sudo bash deploy/install_vm.sh https://github.com/<you>/stonk.git
```

腳本會:裝 Miniconda → clone repo → 建 quant env → 裝依賴 → 註冊 systemd。

接著建立 `/opt/stonk/.env`(從 `.env.example` 複製),補上 GCS 相關設定:

```env
POSITIONS_BACKEND=gcs
POSITIONS_GCS_BUCKET=<與 Cloud Run 同一個 bucket>
GOOGLE_APPLICATION_CREDENTIALS=/opt/stonk/sa-key.json
```

`sa-key.json` 從本機產生後 scp 到 VM:

```bash
gcloud iam service-accounts keys create sa-key.json \
  --iam-account=stonk-positions-sa@$GCP_PROJECT.iam.gserviceaccount.com
scp sa-key.json user@vm:/opt/stonk/sa-key.json
```

完成後啟動服務:

```bash
sudo systemctl restart stonk
sudo journalctl -u stonk -f          # 看即時日誌
```

---

## Web UI 用法

打開 `https://stonk-positions-xxx.run.app/?token=...`:

- 表格顯示所有持倉,每列右側 `×` 按鈕刪除(會跳確認)
- 下方表單新增/更新 — 同 symbol 會直接覆蓋
- 手機友善,可加到主畫面當 PWA 用
- 改完即時生效,VM 端下次掃描就讀到新資料

API 端點:

| 方法 | 路徑 | body | 用途 |
|------|------|------|------|
| GET  | `/`         | -                                 | HTML UI |
| POST | `/add`      | `symbol, entry_price, entry_date` | 新增/更新 |
| POST | `/remove`   | `symbol`                          | 刪除 |
| GET  | `/healthz`  | -                                 | 健康檢查 |

所有請求需帶 `?token=...` 或 `Authorization: Bearer ...`。
