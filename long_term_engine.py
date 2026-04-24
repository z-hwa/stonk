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

# --- 8 項成長指標權重 (黃金比例設計, Fibonacci 降冪) ---
# 相鄰項比值 ≈ φ (1.618)，前兩項佔總權重 62.9%
# 總權重 54，最高加權分數 = 2×54 = 108
GROWTH_WEIGHTS = {
    '收益修正': 21,     # Earnings Revisions
    '獲利驚喜': 13,     # Earnings Surprise
    '營收成長': 8,      # Revenue Growth
    '營業利益率': 5,    # Operating Margin
    '現金流量': 3,      # Cash Flow
    '獲利': 2,          # Net Income
    '獲利動能': 1,      # Earnings Momentum
    'ROE': 1,           # Return on Equity
}

# --- Logger 設定 (延遲建立 FileHandler) ---
logger = logging.getLogger("long_term_engine")
logger.setLevel(logging.DEBUG)


def _ensure_file_handler():
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(
        os.path.join(log_dir, f"longterm_{datetime.now().strftime('%Y%m%d')}.log"),
        mode="a", encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)


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
        """抓取單檔完整基本面資料 (供線程池使用)，涵蓋 8 項成長指標所需資料"""
        try:
            ticker = yf.Ticker(symbol)
            data = {}

            # === 1. 損益表 (營收、淨利、營業利益、EPS) ===
            income = ticker.quarterly_income_stmt
            if income is not None and not income.empty:
                def _find_row(keys):
                    for k in keys:
                        if k in income.index:
                            return income.loc[k].sort_index().dropna()
                    return None

                revenue = _find_row(['Total Revenue', 'Operating Revenue', 'Revenue'])
                ni = _find_row(['Net Income', 'Net Income Common Stockholders',
                                'Net Income Continuous Operations'])
                op_income = _find_row(['Operating Income', 'Total Operating Income As Reported'])
                eps = _find_row(['Diluted EPS', 'Basic EPS'])

                if revenue is not None:
                    data['revenue'] = [float(v) for v in revenue.values[-5:]]
                if ni is not None:
                    data['net_income'] = [float(v) for v in ni.values[-5:]]
                if op_income is not None:
                    data['op_income'] = [float(v) for v in op_income.values[-5:]]
                if eps is not None:
                    data['eps'] = [float(v) for v in eps.values[-5:]]

            # === 2. 現金流量表 ===
            cashflow = ticker.quarterly_cashflow
            if cashflow is not None and not cashflow.empty:
                def _find_cf(keys):
                    for k in keys:
                        if k in cashflow.index:
                            return cashflow.loc[k].sort_index().dropna()
                    return None
                fcf = _find_cf(['Free Cash Flow'])
                op_cf = _find_cf(['Operating Cash Flow', 'Cash Flow From Continuing Operating Activities'])
                if fcf is not None:
                    data['fcf'] = [float(v) for v in fcf.values[-5:]]
                if op_cf is not None:
                    data['op_cf'] = [float(v) for v in op_cf.values[-5:]]

            # === 3. 獲利驚喜 (earnings_history) ===
            try:
                eh = ticker.earnings_history
                if eh is not None and not eh.empty and 'surprisePercent' in eh.columns:
                    surprises = eh['surprisePercent'].dropna().tolist()
                    data['surprise'] = [float(v) for v in surprises[-4:]]
            except Exception:
                pass

            # === 4. 收益修正 (eps_revisions) ===
            try:
                er = ticker.eps_revisions
                if er is not None and not er.empty and '0q' in er.index:
                    # 當季 (0q) 的上修 / 下修
                    row = er.loc['0q']
                    data['eps_rev_up'] = int(row.get('upLast30days', 0) or 0)
                    data['eps_rev_down'] = int(row.get('downLast30days', 0) or 0)
            except Exception:
                pass

            # === 5. ROE (從 info 拿，已預算好) ===
            try:
                info = ticker.info
                roe = info.get('returnOnEquity')
                if roe is not None:
                    data['roe'] = float(roe)
                # 備用：info 的即時營運指標
                for k in ('operatingMargins', 'profitMargins'):
                    v = info.get(k)
                    if v is not None:
                        data[k] = float(v)
            except Exception:
                pass

            if not data.get('revenue'):
                return symbol, None
            return symbol, data
        except Exception:
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

    # --- 成長指標計算 (8 項) ---

    @staticmethod
    def _yoy(series):
        """計算 YoY 成長率 (需要至少 5 季)"""
        if len(series) >= 5 and series[-5] != 0:
            return (series[-1] - series[-5]) / abs(series[-5])
        return None

    @staticmethod
    def _qoq_trend(series):
        """計算季度間成長趨勢"""
        if len(series) < 4:
            return None
        qoq = [(series[i] - series[i-1]) / abs(series[i-1])
               for i in range(1, len(series)) if series[i-1] != 0]
        if not qoq:
            return None
        result = {
            'avg': sum(qoq) / len(qoq),
            'values': qoq,
        }
        if len(qoq) >= 3:
            result['accelerating'] = qoq[-1] > qoq[-2] > qoq[-3]
            result['decelerating'] = qoq[-1] < qoq[-2] < qoq[-3]
        return result

    def _calc_growth_metrics(self, qdata):
        """
        計算 8 項成長指標並產生 growth_score (0-16)
        每項 0-2 分:
          1. 收益修正  2. 獲利驚喜  3. 營收成長  4. 營業利益率
          5. 現金流量  6. 獲利  7. 獲利動能  8. ROE
        """
        if not qdata:
            return None

        m = {}  # metrics
        sub = {}  # 每項的得分 (0-2)

        rev = qdata.get('revenue', [])
        ni = qdata.get('net_income', [])
        eps = qdata.get('eps', [])
        op_income = qdata.get('op_income', [])
        fcf = qdata.get('fcf', [])

        # === 1. 收益修正 (eps_revisions) ===
        up = qdata.get('eps_rev_up', 0)
        down = qdata.get('eps_rev_down', 0)
        m['eps_rev_up'] = up
        m['eps_rev_down'] = down
        if up > down:
            sub['收益修正'] = 2 if up >= 2 * max(down, 1) else 1
        elif down > up:
            sub['收益修正'] = -1  # 下修，後續用於賣出
        else:
            sub['收益修正'] = 0

        # === 2. 獲利驚喜 (earnings_history) ===
        sur = qdata.get('surprise', [])
        if sur:
            m['surprise_last'] = sur[-1]
            m['surprise_avg'] = sum(sur) / len(sur)
            if m['surprise_avg'] > 0.05:
                sub['獲利驚喜'] = 2
            elif m['surprise_last'] > 0:
                sub['獲利驚喜'] = 1
            elif m['surprise_last'] < -0.05:
                sub['獲利驚喜'] = -1  # 連續 miss
            else:
                sub['獲利驚喜'] = 0
        else:
            sub['獲利驚喜'] = 0

        # === 3. 營收成長 ===
        rev_yoy = self._yoy(rev)
        rev_trend = self._qoq_trend(rev)
        if rev_yoy is not None:
            m['rev_yoy'] = rev_yoy
            if rev_yoy > 0.25:
                sub['營收成長'] = 2
            elif rev_yoy > self.growth_min:
                sub['營收成長'] = 1
            elif rev_yoy < 0:
                sub['營收成長'] = -1
            else:
                sub['營收成長'] = 0
        else:
            sub['營收成長'] = 0
        if rev_trend:
            m['rev_accelerating'] = rev_trend.get('accelerating', False)
            m['rev_decelerating'] = rev_trend.get('decelerating', False)

        # === 4. 營業利益率 (Operating Margin) ===
        # 優先用最新季算 (op_income/revenue)，fallback 用 info
        op_margin = None
        if op_income and rev and rev[-1] > 0:
            op_margin = op_income[-1] / rev[-1]
        elif qdata.get('operatingMargins') is not None:
            op_margin = qdata['operatingMargins']
        if op_margin is not None:
            m['op_margin'] = op_margin
            # 檢查是否擴張 (最近季 vs 前 3 季平均)
            op_expanding = False
            if op_income and len(op_income) >= 4 and len(rev) >= 4:
                past_margins = [op_income[i]/rev[i]
                                for i in range(-4, -1) if rev[i] > 0]
                if past_margins:
                    prev_avg = sum(past_margins) / len(past_margins)
                    op_expanding = op_margin > prev_avg * 1.02  # 擴張 >2%
                    m['op_margin_expanding'] = op_expanding
            if op_margin > 0.20:
                sub['營業利益率'] = 2
            elif op_margin > 0.10:
                sub['營業利益率'] = 2 if op_expanding else 1
            elif op_margin > 0:
                sub['營業利益率'] = 1 if op_expanding else 0
            else:
                sub['營業利益率'] = -1
        else:
            sub['營業利益率'] = 0

        # === 5. 現金流量 (Free Cash Flow) ===
        if fcf:
            m['fcf_last'] = fcf[-1]
            fcf_yoy = self._yoy(fcf)
            if fcf_yoy is not None:
                m['fcf_yoy'] = fcf_yoy
            if fcf[-1] > 0:
                # 正向 FCF + 成長
                if fcf_yoy is not None and fcf_yoy > 0.10:
                    sub['現金流量'] = 2
                else:
                    sub['現金流量'] = 1
            else:
                sub['現金流量'] = -1
        else:
            sub['現金流量'] = 0

        # === 6. 獲利 (Net Income) ===
        if ni:
            m['ni_last'] = ni[-1]
            m['ni_negative'] = ni[-1] < 0
            if ni[-1] > 0:
                # 連續 4 季盈利?
                all_positive = all(v > 0 for v in ni[-4:]) if len(ni) >= 4 else False
                m['ni_4q_positive'] = all_positive
                sub['獲利'] = 2 if all_positive else 1
            else:
                sub['獲利'] = -1
            if len(ni) >= 2:
                m['ni_declining'] = ni[-1] < ni[-2]
        else:
            sub['獲利'] = 0

        # === 7. 獲利動能 (Earnings Momentum) ===
        # 優先用 EPS，fallback 用 Net Income
        mom_series = eps if eps else ni
        mom_yoy = self._yoy(mom_series)
        mom_trend = self._qoq_trend(mom_series)
        if mom_yoy is not None:
            m['earnings_momentum_yoy'] = mom_yoy
            if mom_yoy > 0.30:
                sub['獲利動能'] = 2
            elif mom_yoy > self.growth_min:
                # 成長加速額外加分
                sub['獲利動能'] = 2 if mom_trend and mom_trend.get('accelerating') else 1
            elif mom_yoy < 0:
                sub['獲利動能'] = -1
            else:
                sub['獲利動能'] = 0
        else:
            sub['獲利動能'] = 0
        if mom_trend:
            m['earnings_accelerating'] = mom_trend.get('accelerating', False)
            m['earnings_decelerating'] = mom_trend.get('decelerating', False)

        # === 8. ROE ===
        roe = qdata.get('roe')
        if roe is not None:
            m['roe'] = roe
            if roe > 0.25:
                sub['ROE'] = 2
            elif roe > 0.15:
                sub['ROE'] = 1
            elif roe < 0:
                sub['ROE'] = -1
            else:
                sub['ROE'] = 0
        else:
            sub['ROE'] = 0

        # === 彙總：加權成長評分 (越前面越重要) ===
        # 權重 8→1 線性遞減，max = 2×36 = +72, min = -1×36 = -36
        weighted = sum(sub.get(k, 0) * w for k, w in GROWTH_WEIGHTS.items())
        m['growth_score_raw'] = weighted  # 可為負
        m['growth_score'] = max(0, weighted)
        m['growth_score_max'] = 2 * sum(GROWTH_WEIGHTS.values())  # 72
        m['subscores'] = sub

        return m

    # --- 信號評估 ---

    def _evaluate_buy(self, close, market, growth):
        signals = []
        price = float(close.iloc[-1])

        # === 個股/大盤技術面 ===
        # 1. 恐慌買點 (2 分): VIX 恐慌 = 市場超跌 = 機會
        if market and market.get('panic') and not market.get('extreme_panic'):
            signals.append(("VIX恐慌", 2, f"VIX={market['vix_now']:.1f}"))

        # 2. 個股大幅回檔 (2 分)
        high_52w = float(close.tail(252).max())
        drawdown = (price - high_52w) / high_52w
        if drawdown <= -self.drawdown_buy_pct:
            signals.append(("回檔機會", 2, f"距高點 {drawdown*100:+.1f}%"))

        # 3. 長期趨勢仍在 (1 分) - 50 週 MA
        ma250 = close.rolling(250).mean()
        if not pd.isna(ma250.iloc[-1]) and price > float(ma250.iloc[-1]):
            signals.append(("長期多頭", 1, "價格>MA250"))

        # === 基本面：加權 growth_score (0-108) ===
        if growth:
            gs = growth.get('growth_score', 0)
            gs_max = growth.get('growth_score_max', 108)
            sub = growth.get('subscores', {})
            strong_items = [f"{k}({v:+d})"
                            for k, v in sub.items() if v > 0]

            pct = gs / gs_max if gs_max else 0
            if pct >= 0.65:
                signals.append(("基本面強勁", 4,
                                f"GrowthScore={gs}/{gs_max} | " + " ".join(strong_items[:4])))
            elif pct >= 0.40:
                signals.append(("基本面良好", 3,
                                f"GrowthScore={gs}/{gs_max} | " + " ".join(strong_items[:4])))
            elif pct >= 0.20:
                signals.append(("基本面尚可", 1, f"GrowthScore={gs}/{gs_max}"))

        return signals

    def _evaluate_sell(self, close, market, growth):
        """
        賣出評估 (max 約 18 分)

        設計哲學：
          - 單純技術破壞 → 不一定是賣出，可能是買進機會 (價值投資者 buy the dip)
          - 技術破壞 + 基本面疲弱 → 真正危險 (多計 3 分 "雙殺" 確認)
          - 基本面權重 > 技術面權重，符合年尺度投資邏輯

        分層：
          Tier 1 長期趨勢狀態 (合併計分, 0-5 分)
          Tier 2 基本面 (0-4 分)
          Tier 3 雙殺確認 (僅當技術+基本面同時惡化, +3 分)
          Tier 4 大盤輔助 (0-2 分)
          過熱風險 (獨立 2 分)
          成長減速 (獨立 1-2 分)
        """
        signals = []
        price = float(close.iloc[-1])

        # --- 計算技術指標 ---
        ma50 = close.rolling(50).mean()
        ma250 = close.rolling(250).mean()
        ma50_now = float(ma50.iloc[-1]) if not pd.isna(ma50.iloc[-1]) else None
        ma250_now = float(ma250.iloc[-1]) if not pd.isna(ma250.iloc[-1]) else None
        high_52w = float(close.tail(252).max())
        drawdown = (price - high_52w) / high_52w

        # === Tier 1: 長期趨勢狀態 (合併評分, 避免技術訊號過度堆積) ===
        tech_pts = 0
        tech_reasons = []
        if ma250_now is not None and price < ma250_now:
            tech_pts += 2
            tech_reasons.append("價格<MA250")
        if ma50_now is not None and ma250_now is not None and ma50_now < ma250_now:
            tech_pts += 2
            tech_reasons.append("長期死叉")
        if drawdown <= -0.30:
            tech_pts += 1
            tech_reasons.append(f"距高點{drawdown*100:+.1f}%")

        if tech_pts >= 4:
            signals.append(("長期趨勢反轉", tech_pts, " + ".join(tech_reasons)))
        elif tech_pts >= 2:
            signals.append(("趨勢轉弱", tech_pts, " + ".join(tech_reasons)))

        # === 短期動能訊號 (捕捉近期急跌，獨立於長期趨勢) ===
        # 5 日跌幅 > 8%: 急跌
        if len(close) >= 6:
            drop_5d = (price - float(close.iloc[-6])) / float(close.iloc[-6])
            if drop_5d <= -0.08:
                signals.append(("近期急跌", 2, f"5日{drop_5d*100:+.1f}%"))

        # 20 日跌幅 > 15%: 中期動能反轉
        if len(close) >= 21:
            drop_20d = (price - float(close.iloc[-21])) / float(close.iloc[-21])
            if drop_20d <= -0.15:
                signals.append(("中期崩跌", 2, f"20日{drop_20d*100:+.1f}%"))

        # 跌破 MA50 (連續 2 天, 短期趨勢破壞)
        if ma50_now is not None and len(ma50.dropna()) >= 2:
            if price < ma50_now and float(close.iloc[-2]) < float(ma50.iloc[-2]):
                signals.append(("跌破MA50", 1, f"MA50={ma50_now:.2f}"))

        # === 過熱風險 (2 分): 接近新高 + 大盤鬆懈 ===
        if drawdown >= -self.near_high_pct and market and market.get('complacent'):
            signals.append(("過熱風險", 2,
                            f"近高點({drawdown*100:+.1f}%) + VIX鬆懈"))

        # === Tier 2: 基本面惡化 ===
        gs_raw = 0
        if growth:
            gs_raw = growth.get('growth_score_raw', 0)
            gs_max = growth.get('growth_score_max', 108)
            sub = growth.get('subscores', {})
            weak_items = [f"{k}({v:+d})" for k, v in sub.items() if v < 0]

            # 基本面惡化 (4 分): 加權 raw <= -15% max
            if gs_raw <= -gs_max * 0.15:
                signals.append(("基本面惡化", 4,
                                f"Score={gs_raw}/{gs_max} | " + " ".join(weak_items[:4])))
            elif gs_raw <= 0:
                signals.append(("基本面疲弱", 2,
                                f"Score={gs_raw}/{gs_max} | " + " ".join(weak_items[:3])))

            # 成長減速
            rev_dec = growth.get('rev_decelerating', False)
            eps_dec = growth.get('earnings_decelerating', False)
            if rev_dec and eps_dec:
                signals.append(("成長雙減速", 2, "營收+EPS逐季減速"))
            elif rev_dec or eps_dec:
                signals.append(("成長減速", 1,
                                "營收減速" if rev_dec else "獲利減速"))

        # === Tier 3: 雙殺確認 (技術破壞 + 基本面疲弱, +3 分) ===
        # 單純技術破壞可能是買進機會；加上基本面疲弱才是真正危險
        if tech_pts >= 3 and gs_raw <= 0 and growth:
            signals.append(("技術基本面雙殺", 3,
                            "長期趨勢破壞 + 基本面疲弱"))

        # === Tier 4: 大盤輔助 ===
        if market and not market.get('sp_above_ma'):
            signals.append(("大盤走弱", 1, "S&P<MA200"))
        if market and market.get('extreme_panic'):
            signals.append(("極度恐慌", 1, f"VIX={market['vix_now']:.1f}"))

        return signals

    # --- 詳細分析 Log ---

    def _log_detailed_analysis(self, symbol, price, close, market, growth,
                                buy_sigs, sell_sigs,
                                buy_score, sell_score, buy_level, sell_level):
        """輸出詳細分析到 log (多行格式)"""
        lines = []
        lines.append(f"─────── {symbol} @ ${price:.2f} ───────")

        # 結論摘要
        bl = buy_level or "-"
        sl = sell_level or "-"
        lines.append(f"  結論: 買={buy_score}({bl}) | 賣={sell_score}({sl})")

        # 技術面
        ma50 = close.rolling(50).mean()
        ma250 = close.rolling(250).mean()
        ma50_v = float(ma50.iloc[-1]) if not pd.isna(ma50.iloc[-1]) else None
        ma250_v = float(ma250.iloc[-1]) if not pd.isna(ma250.iloc[-1]) else None
        high_52w = float(close.tail(252).max())
        low_52w = float(close.tail(252).min())
        drawdown = (price - high_52w) / high_52w

        tech_parts = [
            f"距高點 {drawdown*100:+.1f}% (高 ${high_52w:.2f})",
            f"距低點 +{(price/low_52w-1)*100:.1f}% (低 ${low_52w:.2f})",
        ]
        if ma50_v:
            tech_parts.append(
                f"MA50 ${ma50_v:.2f} ({'▲' if price > ma50_v else '▼'}{(price/ma50_v-1)*100:+.1f}%)"
            )
        if ma250_v:
            tech_parts.append(
                f"MA250 ${ma250_v:.2f} ({'▲' if price > ma250_v else '▼'}{(price/ma250_v-1)*100:+.1f}%)"
            )
        if ma50_v and ma250_v:
            tech_parts.append(f"長期{'死叉⚠️' if ma50_v < ma250_v else '多頭'}")

        # 短期動能
        if len(close) >= 6:
            drop_5d = (price - float(close.iloc[-6])) / float(close.iloc[-6])
            tech_parts.append(f"5日 {drop_5d*100:+.1f}%")
        if len(close) >= 21:
            drop_20d = (price - float(close.iloc[-21])) / float(close.iloc[-21])
            tech_parts.append(f"20日 {drop_20d*100:+.1f}%")

        lines.append(f"  技術: {' | '.join(tech_parts)}")

        # 大盤環境
        if market:
            mkt_parts = [
                f"S&P {market['sp_now']:.0f} ({'多頭' if market['sp_above_ma'] else '空頭'})",
                f"VIX {market['vix_now']:.1f} ({'😱恐慌' if market['panic'] else '😴鬆懈' if market['complacent'] else '🟡正常'})",
            ]
            lines.append(f"  大盤: {' | '.join(mkt_parts)}")

        # 基本面細節 (8 項)
        if growth:
            gs_raw = growth.get('growth_score_raw', 0)
            gs_max = growth.get('growth_score_max', 108)
            pct = gs_raw / gs_max * 100 if gs_max else 0
            sub = growth.get('subscores', {})

            lines.append(f"  成長評分: {gs_raw:+d}/{gs_max} ({pct:+.0f}%)")
            # 8 項分數表，依權重排序
            for k, w in GROWTH_WEIGHTS.items():
                v = sub.get(k, 0)
                marker = "✅" if v > 0 else ("❌" if v < 0 else "⚪")
                contrib = v * w
                lines.append(f"    {marker} {k:<6s} 權重×{w:>2d}  分數 {v:+d}  貢獻 {contrib:+d}")

            # 基本面原始數據
            raw_parts = []
            if 'rev_yoy' in growth:
                raw_parts.append(f"營收YoY={growth['rev_yoy']*100:+.1f}%")
            if 'earnings_momentum_yoy' in growth:
                raw_parts.append(f"EPS YoY={growth['earnings_momentum_yoy']*100:+.1f}%")
            if 'op_margin' in growth:
                raw_parts.append(f"營業利益率={growth['op_margin']*100:.1f}%")
            if 'roe' in growth:
                raw_parts.append(f"ROE={growth['roe']*100:.1f}%")
            if 'surprise_avg' in growth:
                raw_parts.append(f"獲利驚喜={growth['surprise_avg']*100:+.1f}%")
            if 'eps_rev_up' in growth or 'eps_rev_down' in growth:
                up = growth.get('eps_rev_up', 0)
                down = growth.get('eps_rev_down', 0)
                raw_parts.append(f"分析師 上修{up}/下修{down}")
            if raw_parts:
                lines.append(f"  原始指標: {' | '.join(raw_parts)}")

        # 觸發訊號列表
        if buy_sigs:
            sig_str = " + ".join(f"{n}(+{w})" for n, w, _ in buy_sigs)
            lines.append(f"  買入訊號: {sig_str}")
            for n, w, d in buy_sigs:
                lines.append(f"    • {n} [+{w}]: {d}")
        if sell_sigs:
            sig_str = " + ".join(f"{n}(+{w})" for n, w, _ in sell_sigs)
            lines.append(f"  賣出訊號: {sig_str}")
            for n, w, d in sell_sigs:
                lines.append(f"    • {n} [+{w}]: {d}")

        if not buy_sigs and not sell_sigs:
            lines.append(f"  (無觸發訊號)")

        logger.info("\n".join(lines))

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
        _ensure_file_handler()
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

                # 買入 max ≈ 9 分: STRONG>=7 (78%)
                buy_level = ("STRONG BUY" if buy_score >= 7
                             else "BUY" if buy_score >= self.buy_notify_min
                             else "WATCH" if buy_score >= 1 else None)
                # 賣出 max ≈ 18 分: STRONG>=9 (50%, 需雙殺觸發才會到)
                sell_level = ("STRONG SELL" if sell_score >= 9
                              else "SELL" if sell_score >= self.sell_notify_min
                              else "CAUTION" if sell_score >= 1 else None)

                price = float(close.iloc[-1])

                # === 詳細分析 log (多行結構化輸出) ===
                self._log_detailed_analysis(
                    symbol, price, close, market, growth,
                    buy_sigs, sell_sigs,
                    buy_score, sell_score, buy_level, sell_level
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
