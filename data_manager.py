import yfinance as yf
import pandas as pd
import os
import sqlite3
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from tqdm import tqdm

# 設定路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "dataset/stocks.db")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

# --- Logger (延遲建立 FileHandler) ---
logger = logging.getLogger("data_manager")
logger.setLevel(logging.DEBUG)


def _ensure_file_handler():
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(
        os.path.join(log_dir, f"data_manager_{datetime.now().strftime('%Y%m%d')}.log"),
        mode="a", encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)


def _log(level, msg):
    """同時寫 logger 跟 stdout (stdout 給 systemd journal/tqdm 即時觀察)"""
    print(msg)
    logger.log(level, msg)

# --- 設定 ---
FULL_REFRESH_DAYS = 30   # 超過 N 天沒更新就重新抓 2 年完整資料 (處理拆股/股息調整)
BATCH_SIZE = 100         # 每次批量下載的標的數


def _read_watchlist_from_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT symbol FROM watchlist')
    tickers = [row[0] for row in cursor.fetchall()]
    conn.close()
    return tickers


def _get_last_cached_date(symbol):
    """取得本地 parquet 中最後一筆資料的日期，沒有則回傳 None"""
    path = os.path.join(CACHE_DIR, f"{symbol}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        if df.empty:
            return None
        return df.index.max().date()
    except Exception:
        return None


def _categorize_symbols(symbols):
    """
    把標的分成兩類:
    - full_list: 需要重新抓 2 年完整資料 (新標的或快取太舊)
    - incremental_groups: 只需增量更新 {start_date: [symbols]}
    """
    today = datetime.now().date()
    full_list = []
    incremental_groups = defaultdict(list)

    for sym in symbols:
        last_date = _get_last_cached_date(sym)
        if last_date is None:
            # 全新標的
            full_list.append(sym)
            continue

        days_old = (today - last_date).days

        if days_old <= 1:
            # 今天或昨天的資料 → 跳過 (美股可能尚未收盤，避免 start > end 錯誤)
            continue
        elif days_old > FULL_REFRESH_DAYS:
            # 太久沒更新，重抓全量 (避免漏掉拆股/股息調整)
            full_list.append(sym)
        else:
            # 增量更新：從 last_date+1 抓到今天
            start = last_date + timedelta(days=1)
            incremental_groups[start].append(sym)

    return full_list, incremental_groups


_PRICE_FIELDS = {'Open', 'High', 'Low', 'Close', 'Volume', 'Adj Close'}


def _flatten_columns(df):
    """把 MultiIndex 欄位攤平成單層。
    自動偵測哪一層是 Price 欄位 (yfinance 單檔/多檔下載的層級順序不同)"""
    if df.columns.nlevels > 1:
        df = df.copy()
        # 找出哪一層的值符合 Price 欄位名
        for lvl in range(df.columns.nlevels):
            if set(df.columns.get_level_values(lvl)) & _PRICE_FIELDS:
                df.columns = df.columns.get_level_values(lvl)
                return df
        # fallback: 取第一層
        df.columns = df.columns.get_level_values(0)
    return df


def _save_symbol_data(symbol, df, mode='write'):
    """儲存單檔資料到 parquet。mode='append' 時會與既有資料合併去重。
    所有資料統一存成單層欄位以便合併。"""
    path = os.path.join(CACHE_DIR, f"{symbol}.parquet")

    if df is None or df.empty:
        return False

    df = _flatten_columns(df)

    if mode == 'append' and os.path.exists(path):
        try:
            existing = _flatten_columns(pd.read_parquet(path))
            # 確保兩邊欄位一致
            common_cols = [c for c in existing.columns if c in df.columns]
            existing = existing[common_cols]
            df = df[common_cols]
            # 合併並按 index 去重，新資料優先
            combined = pd.concat([existing, df])
            combined = combined[~combined.index.duplicated(keep='last')]
            combined = combined.sort_index()
            combined.to_parquet(path)
            return True
        except Exception as e:
            _log(logging.WARNING, f"⚠️  {symbol} 合併失敗,改為覆寫: {type(e).__name__}: {e}")

    try:
        df.to_parquet(path)
        return True
    except Exception as e:
        _log(logging.ERROR, f"❌ {symbol} 寫入 parquet 失敗: {type(e).__name__}: {e} (path={path})")
        return False


def _download_batch(batch, period=None, start=None):
    """批量下載並回傳 dict[symbol -> df]"""
    kwargs = dict(
        tickers=batch,
        interval="1d",
        group_by='ticker',
        progress=False,
        threads=True,
        auto_adjust=True,
    )
    if period:
        kwargs['period'] = period
    if start:
        kwargs['start'] = start.strftime('%Y-%m-%d')

    data = yf.download(**kwargs)

    result = {}
    if data is None or data.empty:
        return result

    if len(batch) == 1:
        sym = batch[0]
        result[sym] = data.dropna(how='all')
    else:
        # MultiIndex columns: level 0 = ticker
        if data.columns.nlevels < 2:
            return result
        for sym in batch:
            if sym in data.columns.get_level_values(0):
                result[sym] = data[sym].dropna(how='all')
    return result


def update_local_cache(symbols=None):
    """
    增量更新本地 Parquet 快取。

    Args:
        symbols: 指定要更新的標的清單。None 表示更新 SQLite watchlist 全部。
    """
    _ensure_file_handler()

    if symbols is None:
        symbols = _read_watchlist_from_db()

    if not symbols:
        _log(logging.WARNING, "⚠️ 監控清單為空。")
        return

    _log(logging.INFO, f"[{datetime.now()}] 檢查 {len(symbols)} 檔快取狀態...")

    full_list, incremental_groups = _categorize_symbols(symbols)
    inc_total = sum(len(v) for v in incremental_groups.values())
    skip_count = len(symbols) - len(full_list) - inc_total

    _log(logging.INFO, f"  📊 已最新: {skip_count} | 增量更新: {inc_total} | 全量重抓: {len(full_list)}")

    if not full_list and not incremental_groups:
        _log(logging.INFO, f"[{datetime.now()}] ✅ 所有標的皆為最新狀態。")
        return

    # === 1. 全量下載 (新標的或太舊) ===
    if full_list:
        _log(logging.INFO, f"🌐 全量下載 {len(full_list)} 檔 (period=2y): {full_list}")
        for i in tqdm(range(0, len(full_list), BATCH_SIZE),
                      desc="全量下載", unit="batch"):
            batch = full_list[i:i + BATCH_SIZE]
            try:
                data_dict = _download_batch(batch, period="2y")
                if not data_dict:
                    _log(logging.WARNING,
                         f"⚠️  batch {i}~{i+len(batch)} 全量下載回傳空,ticker={batch}")
                for sym in batch:
                    if sym not in data_dict:
                        _log(logging.WARNING, f"⚠️  {sym} 不在 yfinance 回傳中")
                for sym, df in data_dict.items():
                    _save_symbol_data(sym, df, mode='write')
            except Exception as e:
                _log(logging.ERROR,
                     f"❌ batch {i}~{i+len(batch)} 全量下載例外: {type(e).__name__}: {e}")

    # === 2. 增量下載 (按 start_date 分組批量抓) ===
    if incremental_groups:
        total = sum(len(v) for v in incremental_groups.values())
        _log(logging.INFO, f"⚡ 增量更新 {total} 檔 (按起始日分組)...")

        for start_date, group_syms in incremental_groups.items():
            days_to_fetch = (datetime.now().date() - start_date).days + 1
            _log(logging.INFO, f"  從 {start_date} 開始 ({days_to_fetch} 天): {len(group_syms)} 檔")

            for i in tqdm(range(0, len(group_syms), BATCH_SIZE),
                          desc=f"增量({start_date})", unit="batch"):
                batch = group_syms[i:i + BATCH_SIZE]
                try:
                    data_dict = _download_batch(batch, start=start_date)
                    if not data_dict:
                        _log(logging.WARNING,
                             f"⚠️  batch {i}~{i+len(batch)} 增量下載回傳空,ticker={batch}")
                    for sym, df in data_dict.items():
                        _save_symbol_data(sym, df, mode='append')
                except Exception as e:
                    _log(logging.ERROR,
                         f"❌ batch {i}~{i+len(batch)} 增量下載例外: {type(e).__name__}: {e}")

    _log(logging.INFO, f"[{datetime.now()}] ✨ 快取同步完成。")


if __name__ == "__main__":
    update_local_cache()
