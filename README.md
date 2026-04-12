## 主程式
執行 main.py

## 資料庫設定
執行 init_db.py

### 如何操作你的資料表？
既然你是在 WSL 環境下，你可以直接透過指令來新增或刪除股票，不需要動到程式碼：

進入 SQLite 終端： sqlite3 stocks.db

查看目前的清單： SELECT * FROM watchlist;

新增一檔股票： INSERT INTO watchlist (symbol) VALUES ('GOOGL');

刪除一檔股票： DELETE FROM watchlist WHERE symbol = 'TSLA';