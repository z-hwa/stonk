import sqlite3
import os

def init_db():
    # 使用絕對路徑確保資料庫檔案跟腳本放在一起
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(BASE_DIR, "stocks.db")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 建立標的資料表 (修正語法：NOT NULL)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL UNIQUE,
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 插入初始標的
    initial_stocks = ["NVDA", "TSLA", "AAPL", "AMD", "MSFT"]
    for s in initial_stocks:
        try:
            cursor.execute('INSERT INTO watchlist (symbol) VALUES (?)', (s,))
        except sqlite3.IntegrityError:
            pass
            
    conn.commit()
    conn.close()
    print(f"✅ 資料庫初始化完成！位置：{DB_PATH}")

if __name__ == "__main__":
    init_db()