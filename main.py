import os
import sqlite3
from engine import StockEngine
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime

from data_manager import update_local_cache

def get_watchlist_from_db():
    """從 SQLite 讀取最新的監控清單"""
    try:
        conn = sqlite3.connect('stocks.db')
        cursor = conn.cursor()
        cursor.execute('SELECT symbol FROM watchlist')
        rows = cursor.fetchall()
        conn.close()
        # 將 [(NVDA,), (TSLA,)] 轉換為 [NVDA, TSLA]
        return [row[0] for row in rows]
    except Exception as e:
        print(f"讀取資料庫失敗: {e}")
        return []

def daily_scan_job():
    """包裝原本的掃描邏輯，加入動態讀取清單"""
    # 1. 先做資料同步 (更新本地快取)
    update_local_cache()
    
    current_list = get_watchlist_from_db()
    if not current_list:
        print("⚠️ 監控清單為空，取消本次掃描。")
        return
        
    # 每次掃描時動態初始化引擎，確保使用最新的清單
    engine = StockEngine(current_list)
    engine.run_daily_scan()

def main():
    # 初始化排程器
    scheduler = BlockingScheduler()
    
    # 初始化一個用於發送系統狀態的引擎
    system_engine = StockEngine([]) 

    # 1. 啟動通知
    system_engine.send_discord("系統狀態", "🚀 監控排程系統已啟動\n模式：SQLite 動態清單讀取", color=0x3498db)

    # 2. 每日掃描任務 (週一至週五 早上 06:30)
    daily_scan_job()
    scheduler.add_job(
        daily_scan_job,
        CronTrigger(day_of_week='mon-fri', hour=6, minute=30),
        id="daily_scan_job"
    )

    # 3. 每 6 小時的心跳通知
    scheduler.add_job(
        lambda: system_engine.send_discord("系統心跳", f"💓 系統正常運行中\n時間：`{datetime.now()}`", 0x2ecc71),
        'interval',
        hours=6,
        id="heartbeat_job"
    )

    print("排程器運行中... 使用 SQLite 監控標的。")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        system_engine.send_discord("系統關閉", "⚠️ 監控系統已停止運行。", color=0xe74c3c)
        print("系統關閉")

if __name__ == "__main__":
    main()