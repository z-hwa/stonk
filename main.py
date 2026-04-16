import os
import sqlite3
from engine import StockEngine
from value_engine import ValueEngine
from trade_engine import TradeTimingEngine
from long_term_engine import LongTermEngine
from profit_taking_engine import ProfitTakingEngine

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime

from data_manager import update_local_cache

def get_watchlist_from_db():
    """從 SQLite 讀取最新的監控清單"""
    try:
        conn = sqlite3.connect('dataset/stocks.db')
        cursor = conn.cursor()
        cursor.execute('SELECT symbol FROM watchlist')
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception as e:
        print(f"讀取資料庫失敗: {e}")
        return []

def get_timing_watchlist():
    """從 .env 讀取交易時機監控清單"""
    raw = os.getenv("TIMING_WATCHLIST", "")
    return [s.strip() for s in raw.split(",") if s.strip()]

def get_lt_watchlist():
    """從 .env 讀取長期監控清單 (LT_WATCHLIST 為空則沿用 TIMING_WATCHLIST)"""
    raw = os.getenv("LT_WATCHLIST", "") or os.getenv("TIMING_WATCHLIST", "")
    return [s.strip() for s in raw.split(",") if s.strip()]

def long_term_scan_job():
    """長期 (年尺度) 掃描，每週執行一次"""
    lt_list = get_lt_watchlist()
    if not lt_list:
        return
    update_local_cache(symbols=lt_list)
    lt_engine = LongTermEngine(lt_list)
    lt_engine.run_long_term_scan()

def daily_scan_job():
    """收盤後完整掃描：資料同步 → RSI → 價值 → 交易時機"""
    # 1. 資料同步
    # update_local_cache()

    # 2. 全量掃描
    # current_list = get_watchlist_from_db()
    # if not current_list:
    #     print("⚠️ 監控清單為空，取消本次掃描。")
    #     return

    # engine = StockEngine(current_list)
    # engine.run_daily_scan()

    # v_engine = ValueEngine(current_list)
    # v_engine.run_value_scan()

    # # 3. 交易時機掃描 (少量自選股)
    # timing_scan_job()

def timing_scan_job():
    """盤前交易時機掃描 + 獲利回收 (僅自選股)"""
    timing_list = get_timing_watchlist()
    if not timing_list:
        return
    update_local_cache(symbols=timing_list)  # 只更新自選股

    t_engine = TradeTimingEngine(timing_list)
    t_engine.run_timing_scan()

    pt_engine = ProfitTakingEngine(timing_list)
    pt_engine.run_profit_scan()

from pytz import timezone

def main():
    tw_tz = timezone('Asia/Taipei')
    scheduler = BlockingScheduler(timezone=tw_tz)
    
    system_engine = StockEngine([])

    # 1. 啟動通知
    system_engine.send_discord("系統狀態",
        "🚀 監控排程系統已啟動\n"
        "模式：交易時機 + 獲利回收 + 長期掃描\n"
        f"時區：UTC+8 | 時間：`{datetime.now()}`",
        color=0x3498db)

    # 2. 收盤後完整掃描 (週一至週五 05:30 UTC+8，美股收盤後)
    scheduler.add_job(
        daily_scan_job,
        CronTrigger(day_of_week='mon-fri', hour=5, minute=30),
        id="daily_scan_job"
    )

    # 3. 盤前時機掃描 (週一至週五 21:00 UTC+8，美股 21:30 開盤前)
    timing_scan_job()
    scheduler.add_job(
        timing_scan_job,
        CronTrigger(day_of_week='mon-fri', hour=21, minute=0),
        id="timing_scan_job"
    )

    # 4. 長期 (年尺度) 掃描 (每週日 08:00 UTC+8，週末復盤)
    long_term_scan_job()
    scheduler.add_job(
        long_term_scan_job,
        CronTrigger(day_of_week='sun', hour=8, minute=0),
        id="long_term_scan_job"
    )

    # 4. 心跳通知 (每 6 小時)
    scheduler.add_job(
        lambda: system_engine.send_discord("系統心跳",
            f"💓 系統正常運行中\n時間：`{datetime.now()}`", 0x2ecc71),
        'interval',
        hours=6,
        id="heartbeat_job"
    )

    print("排程器運行中... (UTC+8)")
    print("  05:30 收盤後掃描 | 21:00 盤前(時機+獲利回收) | 週日08:00 長期 | 每6h 心跳")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        system_engine.send_discord("系統關閉", "⚠️ 監控系統已停止運行。", color=0xe74c3c)
        print("系統關閉")

if __name__ == "__main__":
    main()
