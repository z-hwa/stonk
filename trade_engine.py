import pandas as pd
import os
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv
from tqdm import tqdm

from positions_store import get_store

load_dotenv()

# --- Logger 設定 (延遲建立 FileHandler) ---
logger = logging.getLogger("trade_engine")
logger.setLevel(logging.DEBUG)


def _ensure_file_handler():
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(
        os.path.join(log_dir, f"trade_{datetime.now().strftime('%Y%m%d')}.log"),
        mode="a", encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)


class TradeTimingEngine:
    def __init__(self, watchlist):
        self.watchlist = watchlist
        self.cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
        self.positions_store = get_store()

        # 可從 .env 調整的參數
        self.rsi_oversold = int(os.getenv("TIMING_RSI_OVERSOLD", 30))
        self.rsi_overbought = int(os.getenv("TIMING_RSI_OVERBOUGHT", 70))
        self.atr_stop_mult = float(os.getenv("TIMING_ATR_STOP_MULT", 2.0))
        self.take_profit_pct = float(os.getenv("TIMING_TAKE_PROFIT_PCT", 0.30))
        self.bb_period = int(os.getenv("TIMING_BB_PERIOD", 20))
        self.bb_std = float(os.getenv("TIMING_BB_STD", 2.0))
        self.buy_notify_min = int(os.getenv("TIMING_BUY_NOTIFY_MIN", 3))
        self.sell_notify_min = int(os.getenv("TIMING_SELL_NOTIFY_MIN", 3))

        logger.info(f"TradeTimingEngine 初始化: {len(watchlist)} 檔, "
                    f"RSI [{self.rsi_oversold},{self.rsi_overbought}], "
                    f"ATR stop x{self.atr_stop_mult}, TP {self.take_profit_pct*100:.0f}%")

    # --- Data Access ---

    def get_local_data(self, symbol):
        file_path = os.path.join(self.cache_dir, f"{symbol}.parquet")
        if os.path.exists(file_path):
            return pd.read_parquet(file_path)
        return None

    def _extract_ohlcv(self, df):
        """從可能含 MultiIndex 的 DataFrame 中提取 OHLCV Series"""
        if df.columns.nlevels > 1:
            return {
                'open': df['Open'].iloc[:, 0],
                'high': df['High'].iloc[:, 0],
                'low': df['Low'].iloc[:, 0],
                'close': df['Close'].iloc[:, 0],
                'volume': df['Volume'].iloc[:, 0],
            }
        return {
            'open': df['Open'],
            'high': df['High'],
            'low': df['Low'],
            'close': df['Close'],
            'volume': df['Volume'],
        }

    # --- 持倉追蹤 (positions_store: local 檔案 或 GCS) ---

    def _load_positions(self):
        return self.positions_store.load()

    # --- 技術指標計算 ---

    @staticmethod
    def _calc_rsi(close, period=14):
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _calc_macd(close, fast=12, slow=26, signal=9):
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        return macd_line, signal_line

    @staticmethod
    def _calc_bollinger(close, period=20, std_mult=2.0):
        sma = close.rolling(period).mean()
        std = close.rolling(period).std()
        upper = sma + std_mult * std
        lower = sma - std_mult * std
        return upper, sma, lower

    @staticmethod
    def _calc_atr(high, low, close, period=14):
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    # --- 信號評估 ---

    def _evaluate_buy_signals(self, ohlcv):
        """回傳 [(signal_name, weight, detail)]"""
        close = ohlcv['close']
        volume = ohlcv['volume']
        signals = []

        # 1. RSI 超賣 (權重 2)
        rsi = self._calc_rsi(close)
        last_rsi = float(rsi.iloc[-1])
        if not pd.isna(last_rsi) and last_rsi < self.rsi_oversold:
            signals.append(("RSI超賣", 2, f"RSI={last_rsi:.1f}"))

        # 2. MACD 黃金交叉 (零軸下方, 權重 2)
        macd, macd_sig = self._calc_macd(close)
        m_now, ms_now = float(macd.iloc[-1]), float(macd_sig.iloc[-1])
        m_prev, ms_prev = float(macd.iloc[-2]), float(macd_sig.iloc[-2])
        if not any(pd.isna(v) for v in [m_now, ms_now, m_prev, ms_prev]):
            if m_now > ms_now and m_prev <= ms_prev and m_now < 0:
                signals.append(("MACD金叉", 2, "零軸下方"))

        # 3. 布林帶下軌觸碰 (權重 1)
        _, _, bb_lower = self._calc_bollinger(close, self.bb_period, self.bb_std)
        bb_val = float(bb_lower.iloc[-1])
        if not pd.isna(bb_val) and float(close.iloc[-1]) <= bb_val:
            signals.append(("BB下軌", 1, f"下軌={bb_val:.2f}"))

        # 4. 均線支撐反彈 (權重 2)
        sma50 = close.rolling(50).mean()
        sma200 = close.rolling(200).mean()
        s50, s200 = float(sma50.iloc[-1]), float(sma200.iloc[-1])
        price = float(close.iloc[-1])
        if not any(pd.isna(v) for v in [s50, s200]):
            if price > s200 and s50 * 0.98 <= price <= s50 * 1.02:
                signals.append(("MA支撐反彈", 2, f"近MA50={s50:.2f}"))

        # 5. 放量陽線 (權重 1)
        avg_vol = volume.rolling(20).mean()
        vol_now = float(volume.iloc[-1])
        avg_now = float(avg_vol.iloc[-1])
        if not pd.isna(avg_now) and avg_now > 0:
            vol_ratio = vol_now / avg_now
            if vol_ratio > 1.5 and float(close.iloc[-1]) > float(ohlcv['open'].iloc[-1]):
                signals.append(("放量陽線", 1, f"{vol_ratio:.1f}x均量"))

        return signals

    def _evaluate_sell_signals(self, ohlcv, entry_price=None):
        """回傳 [(signal_name, weight, detail)]"""
        close = ohlcv['close']
        high = ohlcv['high']
        low = ohlcv['low']
        signals = []
        price = float(close.iloc[-1])

        # 1. ATR 追蹤止損 (權重 3)
        atr = self._calc_atr(high, low, close)
        atr_val = float(atr.iloc[-1])
        if not pd.isna(atr_val):
            recent_high = float(close.tail(20).max())
            stop_level = recent_high - self.atr_stop_mult * atr_val
            if price < stop_level:
                signals.append(("ATR止損", 3, f"止損={stop_level:.2f},高點={recent_high:.2f}"))

        # 2. RSI 超買 (權重 1)
        rsi = self._calc_rsi(close)
        last_rsi = float(rsi.iloc[-1])
        if not pd.isna(last_rsi) and last_rsi > self.rsi_overbought:
            signals.append(("RSI超買", 1, f"RSI={last_rsi:.1f}"))

        # 3. MACD 死亡交叉 (零軸上方, 權重 2)
        macd, macd_sig = self._calc_macd(close)
        m_now, ms_now = float(macd.iloc[-1]), float(macd_sig.iloc[-1])
        m_prev, ms_prev = float(macd.iloc[-2]), float(macd_sig.iloc[-2])
        if not any(pd.isna(v) for v in [m_now, ms_now, m_prev, ms_prev]):
            if m_now < ms_now and m_prev >= ms_prev and m_now > 0:
                signals.append(("MACD死叉", 2, "零軸上方"))

        # 4. MA50 跌破 — 連續 2 天 (權重 2)
        sma50 = close.rolling(50).mean()
        s50_now = float(sma50.iloc[-1])
        s50_prev = float(sma50.iloc[-2])
        if not any(pd.isna(v) for v in [s50_now, s50_prev]):
            if float(close.iloc[-1]) < s50_now and float(close.iloc[-2]) < s50_prev:
                signals.append(("MA50跌破", 2, f"MA50={s50_now:.2f}"))

        # 5. 到達停利點 (權重 2, 需要 entry_price)
        if entry_price and entry_price > 0:
            gain = (price - entry_price) / entry_price
            if gain >= self.take_profit_pct:
                signals.append(("停利到達", 2,
                                f"+{gain*100:.1f}%(目標{self.take_profit_pct*100:.0f}%)"))

        return signals

    # --- Discord ---

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

    # --- 主掃描 ---

    def run_timing_scan(self):
        _ensure_file_handler()
        print(f"[{datetime.now()}] 啟動交易時機掃描...")
        logger.info("========== 交易時機掃描開始 ==========")

        positions = self._load_positions()
        buy_alerts = []
        sell_alerts = []
        error_count = 0
        skip_count = 0

        pbar = tqdm(self.watchlist, desc="時機掃描", unit="檔",
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")

        for symbol in pbar:
            pbar.set_postfix_str(symbol)
            try:
                df = self.get_local_data(symbol)
                if df is None or df.empty:
                    skip_count += 1
                    logger.debug(f"{symbol}: 無本地資料，跳過")
                    continue

                ohlcv = self._extract_ohlcv(df)
                price = float(ohlcv['close'].iloc[-1])
                entry_price = positions.get(symbol, {}).get('entry_price')

                buy_sigs = self._evaluate_buy_signals(ohlcv)
                sell_sigs = self._evaluate_sell_signals(ohlcv, entry_price)

                buy_score = sum(w for _, w, _ in buy_sigs)
                sell_score = sum(w for _, w, _ in sell_sigs)

                buy_level = ("STRONG BUY" if buy_score >= 5
                             else "BUY" if buy_score >= self.buy_notify_min
                             else "WATCH" if buy_score >= 1 else None)
                sell_level = ("STRONG SELL" if sell_score >= 6
                              else "SELL" if sell_score >= self.sell_notify_min
                              else "CAUTION" if sell_score >= 1 else None)

                # Log 每筆
                buy_names = [n for n, _, _ in buy_sigs]
                sell_names = [n for n, _, _ in sell_sigs]
                logger.info(
                    f"{symbol:6s} | ${price:8.2f} | "
                    f"買={buy_score}({buy_level or '-':>11s}) | "
                    f"賣={sell_score}({sell_level or '-':>12s}) | "
                    f"信號: {buy_names + sell_names}"
                )

                if buy_level and buy_level != "WATCH":
                    buy_alerts.append({
                        'symbol': symbol, 'price': price,
                        'score': buy_score, 'level': buy_level,
                        'signals': buy_sigs,
                    })
                if sell_level and sell_level not in ("CAUTION",):
                    sell_alerts.append({
                        'symbol': symbol, 'price': price,
                        'score': sell_score, 'level': sell_level,
                        'signals': sell_sigs,
                    })

            except Exception as e:
                error_count += 1
                logger.error(f"{symbol} 出錯: {e}")
                pbar.write(f"❌ {symbol}: {str(e)[:60]}")

        pbar.close()

        # --- 結果摘要 ---
        total = len(self.watchlist)
        scanned = total - skip_count - error_count
        summary = (f"時機掃描完成: {total} 檔, 有效 {scanned}, "
                   f"買入 {len(buy_alerts)}, 賣出 {len(sell_alerts)}, 錯誤 {error_count}")
        print(f"✅ {summary}")
        logger.info(summary)

        # Discord: 買入提醒 (綠/藍)
        if buy_alerts:
            buy_alerts.sort(key=lambda x: x['score'], reverse=True)
            lines = []
            for a in buy_alerts:
                emoji = "🟢" if a['level'] == "STRONG BUY" else "🔵"
                details = " + ".join(f"{n}({d})" for n, _, d in a['signals'])
                lines.append(f"{emoji} **{a['symbol']}** [{a['level']}] "
                             f"Score:{a['score']} | ${a['price']:.2f}\n"
                             f"  _{details}_")
            body = f"掃描 {scanned} 檔\n\n" + "\n\n".join(lines)
            color = 0x2ecc71 if any(a['level'] == "STRONG BUY" for a in buy_alerts) else 0x3498db
            self.send_discord("📈 買入時機提醒", body, color=color)

        # Discord: 賣出提醒 (紅/橘)
        if sell_alerts:
            sell_alerts.sort(key=lambda x: x['score'], reverse=True)
            lines = []
            for a in sell_alerts:
                emoji = "🔴" if a['level'] == "STRONG SELL" else "🟠"
                details = " + ".join(f"{n}({d})" for n, _, d in a['signals'])
                lines.append(f"{emoji} **{a['symbol']}** [{a['level']}] "
                             f"Score:{a['score']} | ${a['price']:.2f}\n"
                             f"  _{details}_")
            body = f"掃描 {scanned} 檔\n\n" + "\n\n".join(lines)
            color = 0xe74c3c if any(a['level'] == "STRONG SELL" for a in sell_alerts) else 0xe67e22
            self.send_discord("📉 賣出時機提醒", body, color=color)

        if not buy_alerts and not sell_alerts:
            self.send_discord("⏳ 時機掃描完畢",
                              f"掃描 {scanned} 檔，目前無觸發信號。",
                              color=0x95a5a6)

        logger.info("========== 掃描結束 ==========")


if __name__ == "__main__":
    raw = os.getenv("TIMING_WATCHLIST", "")
    test_list = [s.strip() for s in raw.split(",") if s.strip()]
    if not test_list:
        print("請在 .env 設定 TIMING_WATCHLIST (逗號分隔)")
    else:
        engine = TradeTimingEngine(test_list)
        engine.run_timing_scan()
