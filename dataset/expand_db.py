import sqlite3
import os
import re
from io import StringIO
import pandas as pd
import requests

# 過濾掉權證、認購權、優先股等非普通股的 symbol 後綴
# 例如 AACBR (Rights), AACBW (Warrants), AAPrA (Preferred)
_JUNK_SUFFIX = re.compile(r'[.+\-]?(W|WS|R|RT|U|UN|UNIT|P|PR[A-Z]?)$', re.IGNORECASE)


def _fetch_nasdaq_trader():
    """從 NASDAQ Trader 取得全部美股上市標的 (NASDAQ + NYSE + AMEX)"""
    headers = {"User-Agent": "Mozilla/5.0"}
    tickers = []

    sources = {
        "NASDAQ": {
            "url": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
            "sym_col": 0,   # Symbol
            "etf_col": 6,   # ETF flag
            "test_col": 3,  # Test Issue
        },
        "NYSE/AMEX": {
            "url": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
            "sym_col": 0,   # ACT Symbol
            "etf_col": 4,   # ETF flag
            "test_col": 6,  # Test Issue
        },
    }

    for name, cfg in sources.items():
        try:
            print(f"正在抓取 {name} 上市清單...")
            r = requests.get(cfg["url"], headers=headers, timeout=30)
            r.raise_for_status()
            lines = r.text.strip().splitlines()
            # 跳過 header (第一行) 和 footer (最後一行有 File Creation Time)
            data_lines = lines[1:-1]

            count = 0
            for line in data_lines:
                parts = line.split('|')
                sym = parts[cfg["sym_col"]].strip()
                is_etf = parts[cfg["etf_col"]].strip() == 'Y'
                is_test = parts[cfg["test_col"]].strip() == 'Y'

                if is_etf or is_test:
                    continue
                # 過濾優先股 ($)、權證、認購權等非普通股
                if '$' in sym or _JUNK_SUFFIX.search(sym):
                    continue
                # 過濾長度異常或含空白的 symbol
                if not sym or len(sym) > 5 or ' ' in sym:
                    continue

                tickers.append(sym)
                count += 1

            print(f"✅ {name}: {count} 檔普通股")
        except Exception as e:
            print(f"❌ {name} 抓取失敗: {e}")

    return tickers


def _fetch_sp_indices():
    """從 Wikipedia 抓取 S&P 500 / 400 / 600 成分股 (作為補充來源)"""
    indices = {
        "S&P 500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "S&P 400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
        "S&P 600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    tickers = []

    for name, url in indices.items():
        try:
            print(f"正在抓取 {name}...")
            response = requests.get(url, headers=headers, timeout=30)
            tables = pd.read_html(StringIO(response.text))

            df = None
            for t in tables:
                if 'Symbol' in t.columns or 'Ticker' in t.columns:
                    df = t
                    break

            if df is not None:
                col = 'Symbol' if 'Symbol' in df.columns else 'Ticker'
                syms = df[col].tolist()
                tickers.extend(syms)
                print(f"✅ {name}: {len(syms)} 檔")
        except Exception as e:
            print(f"❌ {name} 抓取失敗: {e}")

    return tickers


def expand_watchlist_robust():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(BASE_DIR, "stocks.db")

    print("🚀 開始從多個來源獲取美股標的...\n")

    # --- 來源 1: NASDAQ Trader (全量) ---
    all_tickers = _fetch_nasdaq_trader()
    print()

    # --- 來源 2: Wikipedia S&P 指數 (補充) ---
    sp_tickers = _fetch_sp_indices()
    all_tickers.extend(sp_tickers)
    print()

    # --- 資料清洗 ---
    # yfinance 格式: '.' → '-' (例如 BRK.B → BRK-B)
    cleaned = set()
    for s in all_tickers:
        s = str(s).strip().replace('.', '-')
        if s and len(s) <= 6:
            cleaned.add(s)

    final_tickers = sorted(cleaned)

    if not final_tickers:
        print("😭 未能獲取任何標的，請檢查網路連線。")
        return

    # --- 寫入資料庫 ---
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 讀取現有清單
    cursor.execute('SELECT symbol FROM watchlist')
    existing = {row[0] for row in cursor.fetchall()}

    new_count = 0
    for s in final_tickers:
        if s not in existing:
            try:
                cursor.execute('INSERT INTO watchlist (symbol) VALUES (?)', (s,))
                new_count += 1
            except sqlite3.IntegrityError:
                pass

    conn.commit()

    # 取得最終總數
    cursor.execute('SELECT COUNT(*) FROM watchlist')
    total = cursor.fetchone()[0]
    conn.close()

    print(f"🎊 擴充完成！新增 {new_count} 檔，清單總數: {total} 檔")


if __name__ == "__main__":
    expand_watchlist_robust()
