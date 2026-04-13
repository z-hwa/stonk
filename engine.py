import yfinance as yf
import pandas as pd
import os
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

# --- Logger 設定 ---
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("stock_engine")
logger.setLevel(logging.DEBUG)

_fh = logging.FileHandler(
    os.path.join(LOG_DIR, f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
    encoding="utf-8"
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)


class StockEngine:
    def __init__(self, watchlist):
        self.watchlist = watchlist
        self.cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    def get_local_data(self, symbol):
        """讀取本地快取資料"""
        file_path = os.path.join(self.cache_dir, f"{symbol}.parquet")
        if os.path.exists(file_path):
            return pd.read_parquet(file_path)
        return None

    def _extract_close_series(self, df):
        """從可能含 MultiIndex 的 DataFrame 中安全提取 Close 欄位為 Series"""
        if df.columns.nlevels > 1:
            return df['Close'].iloc[:, 0]
        return df['Close']

    def send_discord(self, title, description, color=0x3498db):
        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "timestamp": datetime.utcnow().isoformat()
            }]
        }
        try:
            requests.post(self.webhook_url, json=payload, timeout=10)
        except Exception as e:
            print(f"DC 發送失敗: {e}")

    def run_daily_scan(self):
        """執行 RSI 掃描邏輯"""
        print(f"[{datetime.now()}] 啟動 RSI 策略掃描...")
        logger.info("========== RSI 策略掃描開始 ==========")
        signals = []
        error_count = 0
        skip_count = 0

        pbar = tqdm(self.watchlist, desc="RSI 掃描", unit="檔",
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")

        for symbol in pbar:
            pbar.set_postfix_str(symbol)
            try:
                df = self.get_local_data(symbol)
                if df is None or df.empty:
                    skip_count += 1
                    logger.debug(f"{symbol}: 無本地資料，跳過")
                    continue

                close_ser = self._extract_close_series(df)

                # RSI 計算
                delta = close_ser.diff()
                gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                rs = gain / loss
                rsi = 100 - (100 / (1 + rs))

                last_rsi = float(rsi.iloc[-1])
                last_price = float(close_ser.iloc[-1])

                logger.info(f"{symbol:6s} | 價格:{last_price:8.2f} | RSI:{last_rsi:5.1f} | "
                            f"{'SIGNAL' if last_rsi < 35 else '-'}")

                if last_rsi < 35:
                    signals.append({"symbol": symbol, "rsi": last_rsi, "price": last_price})

            except Exception as e:
                error_count += 1
                logger.error(f"{symbol} 掃描出錯: {e}")
                pbar.write(f"❌ {symbol}: {str(e)[:60]}")
                continue

        pbar.close()

        # --- 結果摘要 ---
        total = len(self.watchlist)
        scanned = total - skip_count - error_count
        summary = (f"RSI 掃描完成: 共 {total} 檔, 有效 {scanned} 檔, "
                   f"觸發 {len(signals)} 檔, 跳過 {skip_count} 檔, 錯誤 {error_count} 檔")
        print(f"✅ {summary}")
        logger.info(summary)

        # Discord: 精簡報告
        if signals:
            signals.sort(key=lambda x: x['rsi'])
            report_lines = [
                f"🔍 **{s['symbol']}** — RSI `{s['rsi']:.1f}` | ${s['price']:.2f}"
                for s in signals[:15]
            ]
            body = f"從 {scanned} 檔中觸發 {len(signals)} 檔\n\n" + "\n".join(report_lines)
            self.send_discord("📉 RSI 超賣報告", body, color=0xf1c40f)
            logger.info(f"信號清單: {[s['symbol'] for s in signals]}")
        else:
            self.send_discord("掃描完畢", f"掃描 {scanned} 檔，今日無符合訊號。", color=0x95a5a6)

        logger.info("========== 掃描結束 ==========")
