import yfinance as yf
import pandas as pd
import os
import json
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

# --- Logger 設定 (延遲建立 FileHandler) ---
logger = logging.getLogger("value_engine")
logger.setLevel(logging.DEBUG)


def _ensure_file_handler():
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(
        os.path.join(log_dir, f"value_{datetime.now().strftime('%Y%m%d')}.log"),
        mode="a", encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)


class ValueEngine:
    def __init__(self, watchlist):
        self.watchlist = watchlist
        self.cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
        self.fundamentals_path = os.path.join(self.cache_dir, "_fundamentals.json")
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

        # 從 .env 讀取門檻，並轉換為對應型別 (int/float)
        # 後方的數字是預設值 (Default Value)
        self.mkt_cap_min = float(os.getenv("MKT_CAP_MIN", 10e9))
        self.pe_max = float(os.getenv("PE_MAX", 30))
        self.position_max = float(os.getenv("POSITION_MAX", 0.4))
        self.fund_cache_days = int(os.getenv("FUND_CACHE_DAYS", 7))

        msg = f"載入策略門檻: 市值 > {self.mkt_cap_min/1e9}B, PE < {self.pe_max}, 位階 < {self.position_max}"
        print(f"⚙️ {msg}")
        logger.info(msg)

    def get_local_data(self, symbol):
        file_path = os.path.join(self.cache_dir, f"{symbol}.parquet")
        if os.path.exists(file_path):
            return pd.read_parquet(file_path)
        return None

    def _load_fundamentals_cache(self):
        """載入基本面快取，預設 7 天內有效 (可透過 .env FUND_CACHE_DAYS 調整)"""
        if os.path.exists(self.fundamentals_path):
            try:
                with open(self.fundamentals_path, 'r') as f:
                    cache = json.load(f)
                cache_date = datetime.strptime(cache.get('_date', ''), '%Y-%m-%d')
                age = (datetime.now() - cache_date).days
                if age < self.fund_cache_days:
                    logger.info(f"載入基本面快取 ({len(cache) - 1} 筆, {age} 天前建立, {self.fund_cache_days} 天有效)")
                    return cache
                else:
                    logger.info(f"基本面快取已過期 ({age} 天 >= {self.fund_cache_days} 天)")
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
        return {'_date': datetime.now().strftime('%Y-%m-%d')}

    def _save_fundamentals_cache(self, cache):
        cache['_date'] = datetime.now().strftime('%Y-%m-%d')
        with open(self.fundamentals_path, 'w') as f:
            json.dump(cache, f)

    @staticmethod
    def _fetch_one_fundamental(symbol, max_retries=3):
        """單一標的基本面抓取 (供線程池使用)，含重試"""
        import time as _time
        for attempt in range(max_retries):
            try:
                info = yf.Ticker(symbol).info
                mkt_cap = float(info.get('marketCap') or 0)
                pe = float(info.get('forwardPE') or info.get('trailingPE') or 0)
                return symbol, {'mkt_cap': mkt_cap, 'pe': pe}
            except Exception:
                if attempt < max_retries - 1:
                    _time.sleep(2 ** attempt)  # 1s, 2s, 4s 指數退避
        return symbol, None

    def _prefetch_fundamentals(self, symbols, cache, workers=5):
        """多線程批量抓取基本面資料 (workers 不宜太高，Yahoo 會 401)"""
        need = [s for s in symbols if s not in cache or s == '_date']
        if not need:
            return

        print(f"🌐 需從 Yahoo Finance 取得 {len(need)} 筆基本面 (已快取 {len(cache)-1} 筆)，{workers} 線程並行...")
        logger.info(f"開始多線程抓取基本面: {len(need)} 筆, workers={workers}")

        failed = 0
        save_interval = 100  # 每抓 100 筆存檔一次，防斷線白費
        done = 0

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._fetch_one_fundamental, s): s for s in need}
            for future in tqdm(as_completed(futures), total=len(need),
                               desc="抓取基本面", unit="筆",
                               bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]"):
                symbol, result = future.result()
                done += 1
                if result is not None:
                    cache[symbol] = result
                else:
                    failed += 1
                    logger.warning(f"{symbol}: 基本面抓取失敗")

                # 定期存檔
                if done % save_interval == 0:
                    self._save_fundamentals_cache(cache)

        self._save_fundamentals_cache(cache)
        print(f"💾 基本面快取已儲存 ({len(cache)-1} 筆, {failed} 筆失敗)")
        logger.info(f"基本面抓取完成: 成功 {len(need)-failed}, 失敗 {failed}")

    def _extract_close_series(self, df):
        """從可能含 MultiIndex 的 DataFrame 中安全提取 Close 欄位為 Series"""
        if df.columns.nlevels > 1:
            return df['Close'].iloc[:, 0]
        return df['Close']

    def send_discord(self, title, description, color=0x9b59b6): # 預設紫色代表價值投資
        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "timestamp": datetime.utcnow().isoformat()
            }]
        }
        try:
            requests.post(self.webhook_url, json=payload, timeout=15)
        except Exception as e:
            print(f"DC 發送失敗: {e}")

    def run_value_scan(self):
        _ensure_file_handler()
        print(f"[{datetime.now()}] 啟動價值位階策略掃描...")
        logger.info("========== 價值位階策略掃描開始 ==========")
        signals = []
        error_count = 0
        skip_count = 0

        # --- 階段 1: 批量抓取基本面 (多線程，只抓未快取的) ---
        fund_cache = self._load_fundamentals_cache()
        self._prefetch_fundamentals(self.watchlist, fund_cache)

        # --- 階段 2: 本地掃描 (純快取 + 計算，不再打 API) ---
        pbar = tqdm(self.watchlist, desc="價值掃描", unit="檔",
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")

        for symbol in pbar:
            pbar.set_postfix_str(symbol)
            try:
                # 1. 讀取快取的價格資料
                df = self.get_local_data(symbol)
                if df is None or df.empty:
                    skip_count += 1
                    logger.debug(f"{symbol}: 無本地資料，跳過")
                    continue

                # 2. 安全提取 Close Series
                close_ser = self._extract_close_series(df)
                recent_year = close_ser.tail(252)

                high_1y = float(recent_year.max())
                low_1y = float(recent_year.min())
                current_price = float(close_ser.iloc[-1])

                if high_1y == low_1y:
                    skip_count += 1
                    continue
                position = (current_price - low_1y) / (high_1y - low_1y)

                ma_series = close_ser.rolling(window=200).mean()
                if len(ma_series) < 200 or pd.isna(ma_series.iloc[-1]):
                    ma200 = None
                else:
                    ma200 = float(ma_series.iloc[-1])

                # 3. 從快取讀基本面
                fundamentals = fund_cache.get(symbol, {'mkt_cap': 0, 'pe': 0})
                mkt_cap = fundamentals['mkt_cap']
                pe = fundamentals['pe']

                # 4. 條件判斷
                cap_ok = bool(mkt_cap >= self.mkt_cap_min)
                pe_ok = bool(0 < pe <= self.pe_max)
                pos_ok = bool(position < self.position_max)
                trend_ok = bool(ma200 is not None and current_price > ma200)
                passed = cap_ok and pe_ok and pos_ok and trend_ok

                # 寫入 log (每筆都記錄)
                logger.info(
                    f"{symbol:6s} | 價格:{current_price:8.2f} | 市值:{mkt_cap/1e9:6.1f}B | "
                    f"PE:{pe:6.1f} | 位階:{position*100:5.1f}% | "
                    f"MA200:{'N/A' if ma200 is None else f'{ma200:.2f}':>8s} | "
                    f"{'PASS' if passed else 'FAIL'}"
                )

                if passed:
                    signals.append({
                        "symbol": symbol, "mkt_cap": mkt_cap, "pe": pe,
                        "position": position, "ma200": ma200, "price": current_price
                    })

            except Exception as e:
                error_count += 1
                logger.error(f"{symbol} 掃描出錯: {e}")
                pbar.write(f"❌ {symbol}: {str(e)[:60]}")
                continue

        pbar.close()

        # --- 結果摘要 ---
        total = len(self.watchlist)
        scanned = total - skip_count - error_count
        summary = (f"掃描完成: 共 {total} 檔, 有效 {scanned} 檔, "
                   f"通過 {len(signals)} 檔, 跳過 {skip_count} 檔, 錯誤 {error_count} 檔")
        print(f"✅ {summary}")
        logger.info(summary)

        # Discord: 只發精簡的信號報告
        if signals:
            signals.sort(key=lambda x: x['position'])
            report_lines = [
                f"💎 **{s['symbol']}** — "
                f"${s['mkt_cap']/1e9:.1f}B | PE {s['pe']:.1f} | "
                f"位階 {s['position']*100:.1f}%"
                for s in signals[:10]
            ]
            body = f"從 {scanned} 檔中篩出 {len(signals)} 檔\n\n" + "\n".join(report_lines)
            self.send_discord("🎯 價值選股報告", body, color=0x9b59b6)
            logger.info(f"信號清單: {[s['symbol'] for s in signals]}")
        else:
            self.send_discord("🔍 價值掃描完畢", f"掃描 {scanned} 檔，今日無符合條件標的。", color=0x95a5a6)

        logger.info("========== 掃描結束 ==========")


if __name__ == "__main__":
    import sqlite3
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    conn = sqlite3.connect(os.path.join(BASE_DIR, "dataset/stocks.db"))
    cursor = conn.cursor()
    cursor.execute('SELECT symbol FROM watchlist')
    test_list = [row[0] for row in cursor.fetchall()]
    conn.close()

    engine = ValueEngine(test_list)
    engine.run_value_scan()
