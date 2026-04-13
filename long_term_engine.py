"""
LongTermEngine - 年尺度買賣判斷引擎

策略邏輯：
- 技術面：使用 50 週均線 (≈250日)、52 週高低點，判斷長期趨勢
- 大盤環境：S&P 500 是否在 MA200 之上，VIX 恐慌指數
- 基本面：4 季營收/淨利成長 (YoY 優先, QoQ 為輔)
- 看重「成長加速度」：營收成長率是否逐季提升
"""

import yfinance as yf
import pandas as pd
import os
import json
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

# --- Logger 設定 ---
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("long_term_engine")
logger.setLevel(logging.DEBUG)

_fh = logging.FileHandler(
    os.path.join(LOG_DIR, f"longterm_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
    encoding="utf-8"
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)


class LongTermEngine:
    def __init__(self, watchlist):
        self.watchlist = watchlist
        self.cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
        self.quarterly_cache_path = os.path.join(self.cache_dir, "_quarterly.json")
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

        # === 從 .env 讀取參數 ===
        # 大盤指數 (^GSPC=S&P500, ^IXIC=NASDAQ, ^DJI=Dow)
        self.market_index = os.getenv("LT_MARKET_INDEX", "^GSPC")

        # VIX 門檻
        self.vix_panic = float(os.getenv("LT_VIX_PANIC", 25))         # > 25 = 恐慌 = 買點
        self.vix_extreme = float(os.getenv("LT_VIX_EXTREME", 35))     # > 35 = 極度恐慌 = 防禦
        self.vix_complacent = float(os.getenv("LT_VIX_COMPLACENT", 15))  # < 15 = 鬆懈 = 警訊

        # 個股技術門檻
        self.drawdown_buy_pct = float(os.getenv("LT_DRAWDOWN_BUY_PCT", 0.20))  # 距高點 20% 視為機會
        self.near_high_pct = float(os.getenv("LT_NEAR_HIGH_PCT", 0.05))         # 距高點 5% 內視為新高

        # 基本面成長門檻
        self.growth_min = float(os.getenv("LT_GROWTH_MIN", 0.10))  # YoY 成長 > 10%
        self.qcache_days = int(os.getenv("LT_QUARTERLY_CACHE_DAYS", 14))  # 季報 14 天快取

        # 通知門檻
        self.buy_notify_min = int(os.getenv("LT_BUY_NOTIFY_MIN", 4))
        self.sell_notify_min = int(os.getenv("LT_SELL_NOTIFY_MIN", 4))

        logger.info(
            f"LongTermEngine 初始化: {len(watchlist)} 檔, 大盤={self.market_index}, "
            f"VIX[{self.vix_complacent}/{self.vix_panic}/{self.vix_extreme}], "
            f"成長門檻 {self.growth_min*100:.0f}%"
        )

    # --- 資料存取 ---

    def get_local_data(self, symbol):
        path = os.path.join(self.cache_dir, f"{symbol}.parquet")
        if os.path.exists(path):
            return pd.read_parquet(path)
        return None

    def _extract_close_series(self, df):
        if df.columns.nlevels > 1:
            return df['Close'].iloc[:, 0]
        return df['Close']

    # --- 大盤環境 (S&P 500 + VIX) ---

    def _fetch_market_context(self):
        """抓取大盤指數和 VIX 當前狀態"""
        print("📊 抓取大盤環境 (S&P 500 + VIX)...")
        try:
            sp_data = yf.download(self.market_index, period='2y',
                                  progress=False, auto_adjust=True, threads=False)
            vix_data = yf.download('^VIX', period='2y',
                                   progress=False, auto_adjust=True, threads=False)

            if sp_data.empty or vix_data.empty:
                logger.warning("大盤資料抓取失敗")
                return None

            sp_close = sp_data['Close']
            if hasattr(sp_close, 'ndim') and sp_close.ndim > 1:
                sp_close = sp_close.iloc[:, 0]
            vix_close = vix_data['Close']
            if hasattr(vix_close, 'ndim') and vix_close.ndim > 1:
                vix_close = vix_close.iloc[:, 0]

            sp_now = float(sp_close.iloc[-1])
            sp_ma200 = float(sp_close.rolling(200).mean().iloc[-1])
            sp_high_52w = float(sp_close.tail(252).max())
            vix_now = float(vix_close.iloc[-1])

            ctx = {
                'sp_now': sp_now,
                'sp_ma200': sp_ma200,
                'sp_above_ma': sp_now > sp_ma200,
                'sp_drawdown_pct': (sp_now - sp_high_52w) / sp_high_52w,
                'vix_now': vix_now,
                'panic': vix_now >= self.vix_panic,
                'extreme_panic': vix_now >= self.vix_extreme,
                'complacent': vix_now <= self.vix_complacent,
            }
            print(f"  S&P 500: {sp_now:.0f} (MA200={sp_ma200:.0f}, "
                  f"{'多頭' if ctx['sp_above_ma'] else '空頭'})")
            print(f"  VIX: {vix_now:.1f} ({'恐慌' if ctx['panic'] else '鬆懈' if ctx['complacent'] else '正常'})")
            logger.info(f"市場環境: {ctx}")
            return ctx
        except Exception as e:
            logger.error(f"大盤抓取失敗: {e}")
            return None

    # --- 季度基本面快取 ---

    def _load_quarterly_cache(self):
        if os.path.exists(self.quarterly_cache_path):
            try:
                with open(self.quarterly_cache_path) as f:
                    cache = json.load(f)
                cache_date = datetime.strptime(cache.get('_date', ''), '%Y-%m-%d')
                age = (datetime.now() - cache_date).days
                if age < self.qcache_days:
                    logger.info(f"載入季度快取 ({len(cache)-1} 筆, {age} 天前, {self.qcache_days} 天有效)")
                    return cache
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
        return {'_date': datetime.now().strftime('%Y-%m-%d')}

    def _save_quarterly_cache(self, cache):
        cache['_date'] = datetime.now().strftime('%Y-%m-%d')
        with open(self.quarterly_cache_path, 'w') as f:
            json.dump(cache, f)

    @staticmethod
    def _fetch_one_quarterly(symbol):
        """抓取單檔季度資料 (供線程池使用)"""
        try:
            ticker = yf.Ticker(symbol)
            income = ticker.quarterly_income_stmt
            if income is None or income.empty:
                return symbol, None

            # 找營收欄位 (yfinance 版本不同欄位名可能不同)
            revenue_keys = ['Total Revenue', 'Operating Revenue', 'Revenue']
            ni_keys = ['Net Income', 'Net Income Common Stockholders',
                       'Net Income Continuous Operations']

            def _find_row(keys):
                for k in keys:
                    if k in income.index:
                        return income.loc[k]
                return None

            revenue = _find_row(revenue_keys)
            ni = _find_row(ni_keys)

            if revenue is None:
                return symbol, None

            # 由舊到新排序，取最近 5 季
            revenue = revenue.sort_index().dropna()
            data = {
                'revenue': [float(v) for v in revenue.values[-5:]],
                'revenue_dates': [d.strftime('%Y-%m-%d') for d in revenue.index[-5:]],
            }
            if ni is not None:
                ni = ni.sort_index().dropna()
                data['net_income'] = [float(v) for v in ni.values[-5:]]
                data['ni_dates'] = [d.strftime('%Y-%m-%d') for d in ni.index[-5:]]

            return symbol, data
        except Exception as e:
            return symbol, None

    def _prefetch_quarterly(self, cache, workers=5):
        """多線程抓季度資料 (workers 5 避免 401)"""
        need = [s for s in self.watchlist if s not in cache or s == '_date']
        if not need:
            return
        print(f"🌐 抓取 {len(need)} 檔季度資料 (已快取 {len(cache)-1} 筆)...")
        failed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._fetch_one_quarterly, s): s for s in need}
            for future in tqdm(as_completed(futures), total=len(need),
                               desc="季度資料", unit="檔",
                               bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]"):
                symbol, result = future.result()
                if result is not None:
                    cache[symbol] = result
                else:
                    failed += 1
        self._save_quarterly_cache(cache)
        logger.info(f"季度資料抓取完成: 成功 {len(need)-failed}, 失敗 {failed}")

    # --- 成長指標計算 ---

    def _calc_growth_metrics(self, qdata):
        """從 4-5 季資料計算成長指標"""
        if not qdata:
            return None
        result = {}

        # === 營收 ===
        rev = qdata.get('revenue', [])
        if len(rev) >= 5 and rev[-5] > 0:
            # YoY: 最近季 vs 一年前同季
            result['rev_yoy'] = (rev[-1] - rev[-5]) / abs(rev[-5])

        if len(rev) >= 4:
            # 計算各季 QoQ 成長率
            qoq = [(rev[i] - rev[i-1]) / abs(rev[i-1])
                   for i in range(1, len(rev)) if rev[i-1] != 0]
            if qoq:
                result['rev_avg_qoq'] = sum(qoq) / len(qoq)
                # 成長加速：最近 2 季比前 2 季快
                if len(qoq) >= 3:
                    result['rev_accelerating'] = qoq[-1] > qoq[-2] > qoq[-3]
                    result['rev_decelerating'] = qoq[-1] < qoq[-2] < qoq[-3]

        # === 淨利 ===
        ni = qdata.get('net_income', [])
        if len(ni) >= 5 and ni[-5] != 0:
            result['ni_yoy'] = (ni[-1] - ni[-5]) / abs(ni[-5])

        if len(ni) >= 1:
            result['ni_now'] = ni[-1]
            result['ni_negative'] = ni[-1] < 0
        if len(ni) >= 2:
            result['ni_declining'] = ni[-1] < ni[-2]

        return result

    # --- 信號評估 ---

    def _evaluate_buy(self, close, market, growth):
        signals = []
        price = float(close.iloc[-1])

        # 1. 大盤健康 (1 分)
        if market and market.get('sp_above_ma'):
            signals.append(("大盤多頭", 1, "S&P>MA200"))

        # 2. 恐慌買點 (2 分)
        if market and market.get('panic') and not market.get('extreme_panic'):
            signals.append(("VIX恐慌", 2, f"VIX={market['vix_now']:.1f}"))

        # 3. 個股大幅回檔 (2 分)
        high_52w = float(close.tail(252).max())
        drawdown = (price - high_52w) / high_52w
        if drawdown <= -self.drawdown_buy_pct:
            signals.append(("回檔機會", 2, f"距高點 {drawdown*100:+.1f}%"))

        # 4. 長期趨勢仍在 (1 分) - 50 週 MA
        ma250 = close.rolling(250).mean()
        if not pd.isna(ma250.iloc[-1]) and price > float(ma250.iloc[-1]):
            signals.append(("長期多頭", 1, f"價格>MA250"))

        # 5. 營收成長 YoY (2 分) 或 QoQ fallback (1 分)
        if growth:
            yoy = growth.get('rev_yoy')
            if yoy is not None and yoy > self.growth_min:
                signals.append(("營收YoY成長", 2, f"{yoy*100:+.1f}%"))
            elif growth.get('rev_avg_qoq', 0) > self.growth_min / 4:
                signals.append(("營收QoQ成長", 1,
                                f"avg {growth['rev_avg_qoq']*100:+.1f}%"))

        # 6. 淨利成長 YoY (2 分)
        if growth and growth.get('ni_yoy', 0) > self.growth_min:
            signals.append(("獲利YoY成長", 2, f"{growth['ni_yoy']*100:+.1f}%"))

        # 7. 成長加速 (2 分)
        if growth and growth.get('rev_accelerating'):
            signals.append(("成長加速", 2, "近季逐步加速"))

        return signals

    def _evaluate_sell(self, close, market, growth):
        signals = []
        price = float(close.iloc[-1])

        # 1. 跌破 50 週均線 (2 分)
        ma250 = close.rolling(250).mean()
        if not pd.isna(ma250.iloc[-1]) and price < float(ma250.iloc[-1]):
            signals.append(("跌破MA250", 2, f"MA250={float(ma250.iloc[-1]):.2f}"))

        # 2. 過熱風險 (2 分): 接近新高 + 大盤鬆懈
        high_52w = float(close.tail(252).max())
        from_high = (price - high_52w) / high_52w
        if from_high >= -self.near_high_pct and market and market.get('complacent'):
            signals.append(("過熱風險", 2,
                            f"近高點({from_high*100:+.1f}%) + VIX低"))

        # 3. 營收成長減速 (2 分)
        if growth and growth.get('rev_decelerating'):
            signals.append(("營收減速", 2, "連續減速"))

        # 4. 淨利衰退 (3 分): YoY 負成長
        if growth and growth.get('ni_yoy') is not None and growth['ni_yoy'] < 0:
            signals.append(("獲利衰退", 3, f"YoY={growth['ni_yoy']*100:+.1f}%"))

        # 5. 淨利為負 (3 分)
        if growth and growth.get('ni_negative'):
            signals.append(("近季虧損", 3, f"NI={growth.get('ni_now',0)/1e6:.1f}M"))

        # 6. 大盤跌破 MA200 (1 分): 系統性風險
        if market and not market.get('sp_above_ma'):
            signals.append(("大盤走弱", 1, "S&P<MA200"))

        # 7. VIX 極度恐慌 (1 分): 防禦性減碼
        if market and market.get('extreme_panic'):
            signals.append(("極度恐慌", 1, f"VIX={market['vix_now']:.1f}"))

        return signals

    # --- Discord ---

    def send_discord(self, title, description, color=0x9b59b6):
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

    def run_long_term_scan(self):
        print(f"[{datetime.now()}] 啟動長期 (年尺度) 掃描...")
        logger.info("========== 長期掃描開始 ==========")

        # 階段 1: 大盤環境
        market = self._fetch_market_context()

        # 階段 2: 季度基本面 (多線程預抓)
        qcache = self._load_quarterly_cache()
        self._prefetch_quarterly(qcache)

        # 階段 3: 個股掃描
        buy_alerts = []
        sell_alerts = []
        skip_count = 0
        error_count = 0

        pbar = tqdm(self.watchlist, desc="長期掃描", unit="檔",
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")

        for symbol in pbar:
            pbar.set_postfix_str(symbol)
            try:
                df = self.get_local_data(symbol)
                if df is None or df.empty:
                    skip_count += 1
                    logger.debug(f"{symbol}: 無本地資料")
                    continue

                close = self._extract_close_series(df)
                if len(close) < 60:
                    skip_count += 1
                    logger.debug(f"{symbol}: 資料不足")
                    continue

                qdata = qcache.get(symbol)
                growth = self._calc_growth_metrics(qdata) if qdata else None

                buy_sigs = self._evaluate_buy(close, market, growth)
                sell_sigs = self._evaluate_sell(close, market, growth)
                buy_score = sum(w for _, w, _ in buy_sigs)
                sell_score = sum(w for _, w, _ in sell_sigs)

                buy_level = ("STRONG BUY" if buy_score >= 7
                             else "BUY" if buy_score >= self.buy_notify_min
                             else "WATCH" if buy_score >= 1 else None)
                sell_level = ("STRONG SELL" if sell_score >= 6
                              else "SELL" if sell_score >= self.sell_notify_min
                              else "CAUTION" if sell_score >= 1 else None)

                price = float(close.iloc[-1])
                growth_str = ""
                if growth:
                    if 'rev_yoy' in growth:
                        growth_str += f"營收YoY={growth['rev_yoy']*100:+.1f}% "
                    if 'ni_yoy' in growth:
                        growth_str += f"淨利YoY={growth['ni_yoy']*100:+.1f}%"
                logger.info(
                    f"{symbol:6s} | ${price:8.2f} | "
                    f"買={buy_score}({buy_level or '-':>11s}) | "
                    f"賣={sell_score}({sell_level or '-':>12s}) | "
                    f"{growth_str}"
                )

                if buy_level and buy_level != "WATCH":
                    buy_alerts.append({
                        'symbol': symbol, 'price': price, 'score': buy_score,
                        'level': buy_level, 'signals': buy_sigs, 'growth': growth,
                    })
                if sell_level and sell_level != "CAUTION":
                    sell_alerts.append({
                        'symbol': symbol, 'price': price, 'score': sell_score,
                        'level': sell_level, 'signals': sell_sigs, 'growth': growth,
                    })

            except Exception as e:
                error_count += 1
                logger.error(f"{symbol} 出錯: {e}")
                pbar.write(f"❌ {symbol}: {str(e)[:60]}")

        pbar.close()

        # --- 摘要與通知 ---
        total = len(self.watchlist)
        scanned = total - skip_count - error_count
        summary = (f"長期掃描完成: {total} 檔, 有效 {scanned}, "
                   f"買入 {len(buy_alerts)}, 賣出 {len(sell_alerts)}, 錯誤 {error_count}")
        print(f"✅ {summary}")
        logger.info(summary)

        # 大盤摘要 (放在 Discord 開頭)
        if market:
            mkt_line = (f"📊 **大盤環境**: S&P {market['sp_now']:.0f} "
                        f"({'🟢多頭' if market['sp_above_ma'] else '🔴空頭'}) | "
                        f"VIX {market['vix_now']:.1f} "
                        f"({'😱恐慌' if market['panic'] else '😴鬆懈' if market['complacent'] else '🟡正常'})")
        else:
            mkt_line = "📊 **大盤環境**: 抓取失敗"

        # 買入提醒
        if buy_alerts:
            buy_alerts.sort(key=lambda x: x['score'], reverse=True)
            lines = [mkt_line, ""]
            for a in buy_alerts:
                emoji = "🟢" if a['level'] == "STRONG BUY" else "🔵"
                details = " + ".join(f"{n}({d})" for n, _, d in a['signals'])
                lines.append(
                    f"{emoji} **{a['symbol']}** [{a['level']}] "
                    f"Score:{a['score']} | ${a['price']:.2f}\n"
                    f"  _{details}_"
                )
            color = 0x2ecc71 if any(a['level'] == "STRONG BUY" for a in buy_alerts) else 0x3498db
            self.send_discord("🌱 長期買進機會 (年尺度)", "\n".join(lines), color=color)

        # 賣出提醒
        if sell_alerts:
            sell_alerts.sort(key=lambda x: x['score'], reverse=True)
            lines = [mkt_line, ""]
            for a in sell_alerts:
                emoji = "🔴" if a['level'] == "STRONG SELL" else "🟠"
                details = " + ".join(f"{n}({d})" for n, _, d in a['signals'])
                lines.append(
                    f"{emoji} **{a['symbol']}** [{a['level']}] "
                    f"Score:{a['score']} | ${a['price']:.2f}\n"
                    f"  _{details}_"
                )
            color = 0xe74c3c if any(a['level'] == "STRONG SELL" for a in sell_alerts) else 0xe67e22
            self.send_discord("🍂 長期賣出警訊 (年尺度)", "\n".join(lines), color=color)

        if not buy_alerts and not sell_alerts:
            self.send_discord("📅 長期掃描完畢",
                              f"{mkt_line}\n\n掃描 {scanned} 檔，目前無觸發信號。",
                              color=0x95a5a6)

        logger.info("========== 掃描結束 ==========")


if __name__ == "__main__":
    raw = os.getenv("LT_WATCHLIST") or os.getenv("TIMING_WATCHLIST", "")
    test_list = [s.strip() for s in raw.split(",") if s.strip()]
    if not test_list:
        print("請在 .env 設定 LT_WATCHLIST 或 TIMING_WATCHLIST")
    else:
        engine = LongTermEngine(test_list)
        engine.run_long_term_scan()
