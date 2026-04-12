import yfinance as yf
import pandas as pd
import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

class StockEngine:
    def __init__(self, watchlist):
        self.watchlist = watchlist
        # 確保定義了 cache_dir，使用絕對路徑最穩
        self.cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    def get_local_data(self, symbol):
        """讀取本地快取資料"""
        file_path = os.path.join(self.cache_dir, f"{symbol}.parquet")
        if os.path.exists(file_path):
            return pd.read_parquet(file_path)
        return None

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
        """執行掃描邏輯"""
        print(f"[{datetime.now()}] 啟動策略掃描...")
        signals = []
        
        for symbol in self.watchlist:
            df = self.get_local_data(symbol)
            if df.empty: continue
            
            # 範例 RSI 策略
            delta = df['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            df['RSI'] = 100 - (100 / (1 + rs))
            
            last_rsi = df['RSI'].iloc[-1]
            if last_rsi < 35:
                signals.append(f"🔍 **{symbol}** RSI: `{last_rsi:.2f}`")

        if signals:
            self.send_discord("策略觸發報告", "\n".join(signals), color=0xf1c40f)
        else:
            self.send_discord("掃描完畢", "今日無符合訊號。", color=0x95a5a6)