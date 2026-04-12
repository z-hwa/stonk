import yfinance as yf
import pandas as pd
import os
import sqlite3
from datetime import datetime, timedelta

# 設定路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "stocks.db")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

def update_local_cache():
    """同步 SQLite 清單並下載/更新本地 Parquet 快取"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT symbol FROM watchlist')
    
    # 改成標準寫法，不使用海象運算子
    rows = cursor.fetchall()
    tickers = [row[0] for row in rows]
    
    conn.close()
    
    if not tickers:
        print("⚠️ 監控清單為空，請先在資料庫中加入標的。")
        return

    print(f"[{datetime.now()}] 開始同步 {len(tickers)} 檔標的資料...")

    for symbol in tickers:
        file_path = os.path.join(CACHE_DIR, f"{symbol}.parquet")
        needs_update = True

        # 檢查檔案是否存在且是否為今日已更新
        if os.path.exists(file_path):
            file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
            if file_time.date() == datetime.now().date():
                needs_update = False
        
        if needs_update:
            try:
                # 抓取 2 年資料以確保有足夠空間算 200MA 與 1 年區間
                df = yf.download(symbol, period="2y", interval="1d", progress=False)
                if not df.empty:
                    df.to_parquet(file_path)
                    print(f"✅ {symbol} 快取更新成功")
                else:
                    print(f"⚠️ {symbol} 抓取不到資料")
            except Exception as e:
                print(f"❌ {symbol} 更新失敗: {e}")
        else:
            print(f"平穩: {symbol} 已是最新狀態")

if __name__ == "__main__":
    update_local_cache()