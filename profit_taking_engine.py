"""
ProfitTakingEngine — 獲利回收 + 重新入場引擎

哲學:
  LongTermEngine SELL = 棄守 (基本面壞了)
  ProfitTaking   SELL = 獲利回收 (股票沒問題，只是過熱，等回調買回)

獲利回收只對「已獲利」的持倉評估。
"""

import pandas as pd
import os
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv
from tqdm import tqdm

from positions_store import get_store

load_dotenv()

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("profit_taking")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(
    os.path.join(LOG_DIR, f"profit_take_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
    encoding="utf-8"
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)


class ProfitTakingEngine:
    def __init__(self, watchlist):
        self.watchlist = watchlist
        self.cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
        self.positions_store = get_store()
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

        self.atr_trail_mult = float(os.getenv("PT_ATR_TRAIL_MULT", 2.0))
        self.rsi_overbought = int(os.getenv("PT_RSI_OVERBOUGHT", 75))
        self.ma_deviation_pct = float(os.getenv("PT_MA_DEVIATION_PCT", 0.15))
        self.profit_target_pct = float(os.getenv("PT_PROFIT_TARGET_PCT", 0.30))
        self.sell_notify_min = int(os.getenv("PT_SELL_NOTIFY_MIN", 4))
        self.reentry_notify_min = int(os.getenv("PT_REENTRY_NOTIFY_MIN", 3))

    # --- 技術指標 ---

    @staticmethod
    def _calc_rsi(close, period=14):
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _calc_atr(high, low, close, period=14):
        tr = pd.concat([high - low,
                        (high - close.shift(1)).abs(),
                        (low - close.shift(1)).abs()], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    @staticmethod
    def _calc_bollinger(close, period=20, std_mult=2.0):
        sma = close.rolling(period).mean()
        std = close.rolling(period).std()
        return sma + std_mult * std, sma, sma - std_mult * std

    # --- 獲利回收訊號 ---

    def evaluate_profit_take(self, ohlcv, entry_price):
        """
        評估是否該獲利了結。只對已獲利部位。
        回傳 [(signal_name, weight, detail)]
        """
        close = ohlcv['close']
        high = ohlcv['high']
        low = ohlcv['low']
        volume = ohlcv['volume']
        price = float(close.iloc[-1])
        signals = []

        # 前提: 必須獲利
        unrealized_pct = (price - entry_price) / entry_price
        if unrealized_pct <= 0:
            return signals

        # 1. 動態停利 ATR (3 分)
        atr = self._calc_atr(high, low, close)
        if not pd.isna(atr.iloc[-1]):
            recent_high = float(close.tail(20).max())
            stop = recent_high - self.atr_trail_mult * float(atr.iloc[-1])
            if price < stop:
                signals.append(("ATR停利", 3,
                                f"止損={stop:.2f},高={recent_high:.2f}"))

        # 2. RSI 過熱 (2 分)
        rsi = self._calc_rsi(close)
        last_rsi = float(rsi.iloc[-1])
        if not pd.isna(last_rsi) and last_rsi > self.rsi_overbought:
            signals.append(("RSI過熱", 2, f"RSI={last_rsi:.1f}"))

        # 3. 量價背離 (2 分): 價格創新高但量萎縮
        if len(close) >= 20:
            is_price_high = price >= float(close.tail(20).max())
            avg_vol = float(volume.rolling(20).mean().iloc[-1])
            cur_vol = float(volume.iloc[-1])
            if is_price_high and avg_vol > 0 and cur_vol < avg_vol * 0.8:
                signals.append(("量價背離", 2,
                                f"新高但量={cur_vol/avg_vol:.1f}x均量"))

        # 4. 均線乖離過大 (2 分)
        ma50 = close.rolling(50).mean()
        if not pd.isna(ma50.iloc[-1]):
            deviation = (price - float(ma50.iloc[-1])) / float(ma50.iloc[-1])
            if deviation >= self.ma_deviation_pct:
                signals.append(("均線乖離", 2,
                                f"偏離MA50 {deviation*100:+.1f}%"))

        # 5. 布林帶上軌突破後回落 (1 分)
        bb_upper, _, _ = self._calc_bollinger(close)
        if len(close) >= 2 and not pd.isna(bb_upper.iloc[-1]):
            prev_above = float(close.iloc[-2]) > float(bb_upper.iloc[-2])
            now_below = price <= float(bb_upper.iloc[-1])
            if prev_above and now_below:
                signals.append(("BB上軌回落", 1, f"BB上軌={float(bb_upper.iloc[-1]):.2f}"))

        # 6. 漲幅達標 (1 分)
        if unrealized_pct >= self.profit_target_pct:
            signals.append(("漲幅達標", 1,
                            f"+{unrealized_pct*100:.1f}%(目標{self.profit_target_pct*100:.0f}%)"))

        return signals

    # --- 重新入場訊號 ---

    def evaluate_reentry(self, ohlcv):
        """
        評估是否該重新入場 (獲利了結後等待的標的)。
        回傳 [(signal_name, weight, detail)]
        """
        close = ohlcv['close']
        volume = ohlcv['volume']
        price = float(close.iloc[-1])
        signals = []

        # 1. 回踩 MA50 支撐 (2 分)
        ma50 = close.rolling(50).mean()
        if not pd.isna(ma50.iloc[-1]):
            ma50_v = float(ma50.iloc[-1])
            if ma50_v * 0.98 <= price <= ma50_v * 1.02:
                signals.append(("MA50支撐", 2, f"MA50={ma50_v:.2f}"))

        # 2. RSI 冷卻 (2 分)
        rsi = self._calc_rsi(close)
        last_rsi = float(rsi.iloc[-1])
        if not pd.isna(last_rsi) and 40 <= last_rsi <= 55:
            signals.append(("RSI冷卻", 2, f"RSI={last_rsi:.1f}"))

        # 3. 布林帶中軌回踩 (1 分)
        _, bb_mid, _ = self._calc_bollinger(close)
        if not pd.isna(bb_mid.iloc[-1]):
            bb_mid_v = float(bb_mid.iloc[-1])
            if bb_mid_v * 0.98 <= price <= bb_mid_v * 1.02:
                signals.append(("BB中軌回踩", 1, f"中軌={bb_mid_v:.2f}"))

        # 4. 量縮企穩 (1 分): 連續 3 天跌幅收窄 + 量 < 均量
        if len(close) >= 4:
            daily_ret = close.pct_change()
            last3 = daily_ret.iloc[-3:].abs()
            narrowing = float(last3.iloc[-1]) < float(last3.iloc[-2]) < float(last3.iloc[-3])
            avg_vol = float(volume.rolling(20).mean().iloc[-1])
            low_vol = float(volume.iloc[-1]) < avg_vol if avg_vol > 0 else False
            if narrowing and low_vol:
                signals.append(("量縮企穩", 1, "波幅收窄+量縮"))

        # 5. 長期趨勢仍在 (2 分)
        ma250 = close.rolling(250).mean()
        if not pd.isna(ma50.iloc[-1]) and not pd.isna(ma250.iloc[-1]):
            if price > float(ma250.iloc[-1]) and float(ma50.iloc[-1]) > float(ma250.iloc[-1]):
                signals.append(("長期多頭", 2, "MA50>MA250"))

        return signals

    # --- 提取 OHLCV ---

    def _extract_ohlcv(self, df):
        if df.columns.nlevels > 1:
            return {k: df[k.capitalize()].iloc[:, 0]
                    for k in ['open', 'high', 'low', 'close', 'volume']}
        return {k: df[k.capitalize()] for k in ['open', 'high', 'low', 'close', 'volume']}

    # --- Discord ---

    def send_discord(self, title, description, color=0xf39c12):
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

    # --- 主掃描 (線上版) ---

    def _load_ohlcv(self, symbol):
        file_path = os.path.join(self.cache_dir, f"{symbol}.parquet")
        if not os.path.exists(file_path):
            return None
        df = pd.read_parquet(file_path)
        if df.empty:
            return None
        return self._extract_ohlcv(df)

    def run_profit_scan(self):
        """獲利回收以 GCP 持倉為準,重新入場以 watchlist − 持倉 為準"""
        print(f"[{datetime.now()}] 啟動獲利回收掃描...")
        logger.info("========== 獲利回收掃描開始 ==========")

        positions = self.positions_store.load()
        held_symbols = sorted(positions.keys())
        reentry_symbols = sorted(set(self.watchlist) - set(held_symbols))

        take_profit_alerts = []
        reentry_alerts = []

        # --- 1) 已持倉: 評估獲利回收 ---
        pbar = tqdm(held_symbols, desc="獲利回收", unit="檔",
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")
        for symbol in pbar:
            pbar.set_postfix_str(symbol)
            try:
                ohlcv = self._load_ohlcv(symbol)
                if ohlcv is None:
                    logger.warning(f"{symbol} 持倉但缺快取,跳過")
                    continue
                price = float(ohlcv['close'].iloc[-1])
                entry_price = positions[symbol].get('entry_price')
                if not entry_price or entry_price <= 0:
                    logger.warning(f"{symbol} 持倉缺 entry_price,跳過")
                    continue

                sigs = self.evaluate_profit_take(ohlcv, entry_price)
                score = sum(w for _, w, _ in sigs)
                if score >= self.sell_notify_min:
                    take_profit_alerts.append({
                        'symbol': symbol, 'price': price,
                        'entry': entry_price, 'score': score,
                        'pnl_pct': (price - entry_price) / entry_price,
                        'signals': sigs,
                    })
                logger.info(f"{symbol:6s} | 持倉 ${entry_price:.2f}→${price:.2f} "
                           f"({(price-entry_price)/entry_price*100:+.1f}%) | "
                           f"PT_SELL={score} | {[n for n,_,_ in sigs]}")
            except Exception as e:
                logger.error(f"{symbol} 獲利回收評估出錯: {e}")
        pbar.close()

        # --- 2) 非持倉 watchlist: 評估重新入場 ---
        pbar = tqdm(reentry_symbols, desc="重新入場", unit="檔",
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")
        for symbol in pbar:
            pbar.set_postfix_str(symbol)
            try:
                ohlcv = self._load_ohlcv(symbol)
                if ohlcv is None:
                    continue
                price = float(ohlcv['close'].iloc[-1])
                sigs = self.evaluate_reentry(ohlcv)
                score = sum(w for _, w, _ in sigs)
                if score >= self.reentry_notify_min:
                    reentry_alerts.append({
                        'symbol': symbol, 'price': price,
                        'score': score, 'signals': sigs,
                    })
                logger.info(f"{symbol:6s} | 未持倉 ${price:.2f} | "
                           f"REENTRY={score} | {[n for n,_,_ in sigs]}")
            except Exception as e:
                logger.error(f"{symbol} 重新入場評估出錯: {e}")
        pbar.close()

        # Discord
        if take_profit_alerts:
            take_profit_alerts.sort(key=lambda x: x['score'], reverse=True)
            lines = []
            for a in take_profit_alerts:
                details = " + ".join(f"{n}({d})" for n, _, d in a['signals'])
                lines.append(f"💰 **{a['symbol']}** Score:{a['score']} | "
                             f"${a['entry']:.2f}→${a['price']:.2f} "
                             f"({a['pnl_pct']*100:+.1f}%)\n  _{details}_")
            self.send_discord("[PROFIT] 💰 獲利回收提醒",
                              "\n\n".join(lines), color=0xf39c12)

        if reentry_alerts:
            reentry_alerts.sort(key=lambda x: x['score'], reverse=True)
            lines = []
            for a in reentry_alerts:
                details = " + ".join(f"{n}({d})" for n, _, d in a['signals'])
                lines.append(f"🔄 **{a['symbol']}** Score:{a['score']} | "
                             f"${a['price']:.2f}\n  _{details}_")
            self.send_discord("[PROFIT] 🔄 重新入場提醒",
                              "\n\n".join(lines), color=0x2ecc71)

        self.send_discord("[PROFIT] 獲利策略", "\n\n掃描完成")
        logger.info("========== 掃描結束 ==========")


if __name__ == "__main__":
    raw = os.getenv("LT_WATCHLIST") or os.getenv("TIMING_WATCHLIST", "")
    test_list = [s.strip() for s in raw.split(",") if s.strip()]
    if not test_list:
        print("請在 .env 設定 TIMING_WATCHLIST")
    else:
        engine = ProfitTakingEngine(test_list)
        engine.run_profit_scan()
