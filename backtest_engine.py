"""
BacktestEngine - 回測 LongTermEngine 策略在過去 3 年的表現

限制 (誠實標註):
  - 存活偏差: 用當前 watchlist, 未考慮已下市/剔除
  - 分析師訊號 (收益修正/獲利驚喜) 無歷史快照 → 回測時停用
  - 基本面用年度數據近似 (annual income_stmt, 最近 5 年)
  - 無滑價, 以收盤價成交
"""

import yfinance as yf
import pandas as pd
import numpy as np
import os
import json
import logging
import csv
from datetime import datetime, timedelta
from dotenv import load_dotenv
from tqdm import tqdm

from long_term_engine import LongTermEngine, GROWTH_WEIGHTS

load_dotenv()

# --- 路徑 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
BT_CACHE_DIR = os.path.join(BASE_DIR, "cache", "backtest")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(BT_CACHE_DIR, exist_ok=True)

# --- Logger ---
_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
logger = logging.getLogger("backtest")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(os.path.join(LOG_DIR, f"backtest_{_ts}.log"), encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)

# 回測時可用的指標 (排除無歷史的 2 項)
BT_DISABLED_METRICS = {'收益修正', '獲利驚喜'}
BT_USABLE_WEIGHTS = {k: v for k, v in GROWTH_WEIGHTS.items()
                    if k not in BT_DISABLED_METRICS}
BT_GROWTH_MAX = 2 * sum(BT_USABLE_WEIGHTS.values())  # 40


# ======================================================
# 資料層
# ======================================================
class BacktestDataLoader:
    """離線下載 + 快取 5 年資料 (價格 / 大盤 / 年度基本面)"""

    def __init__(self, years=5):
        self.years = years
        self.prices = {}           # sym -> DataFrame
        self.sp_close = None       # S&P 500 close series
        self.vix_close = None      # VIX close series
        self.spy_close = None      # SPY close (買進持有基準)
        self.annual = {}           # sym -> {year: {revenue, ni, op_income, fcf, equity}}

    def _price_path(self, symbol):
        return os.path.join(BT_CACHE_DIR, f"{symbol}.parquet")

    def _annual_path(self):
        return os.path.join(BT_CACHE_DIR, "_annual.json")

    def prepare_prices(self, symbols):
        """下載個股 + 大盤 5y OHLCV"""
        print(f"📥 下載 {len(symbols)} 檔個股 + 大盤指數 ({self.years}y)...")

        tickers = list(symbols) + ['^GSPC', '^VIX', 'SPY']
        need = []
        for sym in tickers:
            path = self._price_path(sym)
            if os.path.exists(path):
                try:
                    df = pd.read_parquet(path)
                    if df.index.max().date() >= datetime.now().date() - timedelta(days=3):
                        continue  # 已是最新
                except Exception:
                    pass
            need.append(sym)

        if need:
            for sym in tqdm(need, desc="下載價格", unit="檔"):
                try:
                    data = yf.download(sym, period=f'{self.years}y',
                                       progress=False, auto_adjust=True, threads=False)
                    if data is None or data.empty:
                        continue
                    if data.columns.nlevels > 1:
                        data = data.copy()
                        # flatten
                        for lvl in range(data.columns.nlevels):
                            if 'Close' in data.columns.get_level_values(lvl):
                                data.columns = data.columns.get_level_values(lvl)
                                break
                    data.to_parquet(self._price_path(sym))
                except Exception as e:
                    logger.error(f"{sym} 價格下載失敗: {e}")

        # 載入到記憶體
        for sym in symbols:
            path = self._price_path(sym)
            if os.path.exists(path):
                self.prices[sym] = pd.read_parquet(path)

        sp_df = pd.read_parquet(self._price_path('^GSPC'))
        self.sp_close = sp_df['Close']
        vix_df = pd.read_parquet(self._price_path('^VIX'))
        self.vix_close = vix_df['Close']
        spy_df = pd.read_parquet(self._price_path('SPY'))
        self.spy_close = spy_df['Close']

    def prepare_fundamentals(self, symbols):
        """抓取年度基本面 (income_stmt + balance_sheet + cashflow)"""
        cache_path = self._annual_path()

        # 檢查快取
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    cache = json.load(f)
                cache_age = (datetime.now() -
                             datetime.strptime(cache.get('_date', '2000-01-01'), '%Y-%m-%d')).days
                if cache_age < 30 and all(s in cache for s in symbols):
                    print(f"📦 使用年度基本面快取 ({cache_age} 天前)")
                    self.annual = {k: v for k, v in cache.items() if k != '_date'}
                    return
            except Exception:
                pass

        print(f"📥 下載 {len(symbols)} 檔年度基本面...")
        self.annual = {}
        for sym in tqdm(symbols, desc="年度基本面", unit="檔"):
            try:
                t = yf.Ticker(sym)
                inc = t.income_stmt
                cf = t.cashflow
                bs = t.balance_sheet

                if inc is None or inc.empty:
                    continue

                # 提取每年數據, key = year-month-day 字串
                def _extract(df, keys):
                    for k in keys:
                        if df is not None and not df.empty and k in df.index:
                            return df.loc[k]
                    return None

                revenue = _extract(inc, ['Total Revenue', 'Operating Revenue'])
                ni = _extract(inc, ['Net Income', 'Net Income Common Stockholders'])
                op_income = _extract(inc, ['Operating Income'])
                fcf = _extract(cf, ['Free Cash Flow'])
                equity = _extract(bs, ['Stockholders Equity', 'Total Equity Gross Minority Interest'])

                sym_data = {}
                for series, name in [(revenue, 'revenue'), (ni, 'ni'),
                                     (op_income, 'op_income'), (fcf, 'fcf'),
                                     (equity, 'equity')]:
                    if series is not None:
                        for date, val in series.items():
                            if pd.isna(val):
                                continue
                            d = date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date)
                            sym_data.setdefault(d, {})[name] = float(val)

                if sym_data:
                    self.annual[sym] = sym_data
            except Exception as e:
                logger.error(f"{sym} 年度基本面失敗: {e}")

        cache = dict(self.annual)
        cache['_date'] = datetime.now().strftime('%Y-%m-%d')
        with open(cache_path, 'w') as f:
            json.dump(cache, f)

    def prepare_all(self, symbols):
        self.prepare_prices(symbols)
        self.prepare_fundamentals(symbols)

    # === Point-in-time 存取 ===

    def get_price_upto(self, symbol, end_date):
        """回傳 symbol 在 end_date (含) 之前的收盤價序列"""
        df = self.prices.get(symbol)
        if df is None or df.empty:
            return None
        close = df['Close']
        if hasattr(close, 'ndim') and close.ndim > 1:
            close = close.iloc[:, 0]
        return close.loc[:end_date]

    def get_market_context_at(self, end_date):
        """計算 end_date 當下的大盤狀態"""
        sp = self.sp_close.loc[:end_date]
        vix = self.vix_close.loc[:end_date]
        if len(sp) < 200 or len(vix) < 1:
            return None
        sp_now = float(sp.iloc[-1])
        sp_ma200 = float(sp.rolling(200).mean().iloc[-1])
        vix_now = float(vix.iloc[-1])
        return {
            'sp_now': sp_now,
            'sp_ma200': sp_ma200,
            'sp_above_ma': sp_now > sp_ma200,
            'sp_drawdown_pct': (sp_now - float(sp.tail(252).max())) / float(sp.tail(252).max()),
            'vix_now': vix_now,
            'panic': vix_now >= 25,
            'extreme_panic': vix_now >= 35,
            'complacent': vix_now <= 15,
        }

    def get_historical_growth(self, symbol, end_date):
        """用該日期前可得的年度數據計算 growth 指標 (8 項中 6 項)"""
        sym_data = self.annual.get(symbol)
        if not sym_data:
            return None

        # 篩出 end_date 前的年度報告 (假設財報公布延遲 60 天保守估計)
        cutoff = end_date - timedelta(days=60)
        dated = []
        for d, vals in sym_data.items():
            try:
                dt = datetime.strptime(d, '%Y-%m-%d').date()
                if dt <= cutoff.date() if hasattr(cutoff, 'date') else cutoff:
                    dated.append((dt, vals))
            except Exception:
                continue
        dated.sort(key=lambda x: x[0])  # 由舊到新

        if len(dated) < 2:
            return None

        # 取年度序列 (近 5 年, 最新在後)
        def _series(key):
            return [v.get(key) for _, v in dated if v.get(key) is not None]

        rev = _series('revenue')
        ni = _series('ni')
        op_income = _series('op_income')
        fcf = _series('fcf')
        equity = _series('equity')

        sub = {k: 0 for k in GROWTH_WEIGHTS}
        m = {}

        # 3. 營收成長 (年度 YoY)
        if len(rev) >= 2 and rev[-2] > 0:
            rev_yoy = (rev[-1] - rev[-2]) / abs(rev[-2])
            m['rev_yoy'] = rev_yoy
            if rev_yoy > 0.25: sub['營收成長'] = 2
            elif rev_yoy > 0.10: sub['營收成長'] = 1
            elif rev_yoy < 0: sub['營收成長'] = -1

        # 年度 QoQ trend (年度間減速/加速)
        if len(rev) >= 4:
            growth_rates = [(rev[i]-rev[i-1])/abs(rev[i-1])
                           for i in range(1, len(rev)) if rev[i-1] != 0]
            if len(growth_rates) >= 3:
                m['rev_accelerating'] = growth_rates[-1] > growth_rates[-2] > growth_rates[-3]
                m['rev_decelerating'] = growth_rates[-1] < growth_rates[-2] < growth_rates[-3]

        # 4. 營業利益率
        if op_income and rev and rev[-1] > 0:
            op_margin = op_income[-1] / rev[-1]
            m['op_margin'] = op_margin
            # 檢查擴張
            if len(op_income) >= 3 and len(rev) >= 3:
                past = [op_income[i]/rev[i] for i in range(-3, -1) if rev[i] > 0]
                expanding = past and op_margin > sum(past)/len(past) * 1.02
                if op_margin > 0.20: sub['營業利益率'] = 2
                elif op_margin > 0.10: sub['營業利益率'] = 2 if expanding else 1
                elif op_margin > 0: sub['營業利益率'] = 1 if expanding else 0
                else: sub['營業利益率'] = -1

        # 5. 現金流量
        if fcf:
            if fcf[-1] > 0:
                if len(fcf) >= 2 and fcf[-2] > 0 and (fcf[-1]-fcf[-2])/abs(fcf[-2]) > 0.10:
                    sub['現金流量'] = 2
                else:
                    sub['現金流量'] = 1
            else:
                sub['現金流量'] = -1

        # 6. 獲利 (淨利)
        if ni:
            m['ni_negative'] = ni[-1] < 0
            if ni[-1] > 0:
                sub['獲利'] = 2 if all(v > 0 for v in ni[-3:]) else 1
            else:
                sub['獲利'] = -1
            if len(ni) >= 2:
                m['ni_declining'] = ni[-1] < ni[-2]

        # 7. 獲利動能 (YoY)
        if len(ni) >= 2 and ni[-2] != 0:
            ni_yoy = (ni[-1] - ni[-2]) / abs(ni[-2])
            m['earnings_momentum_yoy'] = ni_yoy
            if ni_yoy > 0.30:
                sub['獲利動能'] = 2
            elif ni_yoy > 0.10:
                sub['獲利動能'] = 2 if m.get('rev_accelerating') else 1
            elif ni_yoy < 0:
                sub['獲利動能'] = -1
            if len(ni) >= 4:
                growth_rates = [(ni[i]-ni[i-1])/abs(ni[i-1])
                               for i in range(1, len(ni)) if ni[i-1] != 0]
                if len(growth_rates) >= 3:
                    m['earnings_accelerating'] = growth_rates[-1] > growth_rates[-2] > growth_rates[-3]
                    m['earnings_decelerating'] = growth_rates[-1] < growth_rates[-2] < growth_rates[-3]

        # 8. ROE (ni / equity)
        if ni and equity and equity[-1] > 0:
            roe = ni[-1] / equity[-1]
            m['roe'] = roe
            if roe > 0.25: sub['ROE'] = 2
            elif roe > 0.15: sub['ROE'] = 1
            elif roe < 0: sub['ROE'] = -1

        # 彙總加權分數 (使用 full GROWTH_WEIGHTS，停用項目 = 0)
        weighted = sum(sub.get(k, 0) * w for k, w in GROWTH_WEIGHTS.items())
        m['growth_score_raw'] = weighted
        m['growth_score'] = max(0, weighted)
        m['growth_score_max'] = BT_GROWTH_MAX   # 回測專用 (40)
        m['subscores'] = sub
        return m


# ======================================================
# 投資組合模擬
# ======================================================
class BacktestPortfolio:
    def __init__(self, initial_cash=100_000, max_positions=10,
                 position_size_pct=0.10, cost_pct=0.001, cooldown_days=14):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.max_positions = max_positions
        self.position_size_pct = position_size_pct
        self.cost_pct = cost_pct
        self.cooldown_days = cooldown_days

        self.positions = {}  # sym -> {shares, entry_price, entry_date, entry_value}
        self.cooldown = {}   # sym -> date可再買日
        self.trades = []     # 完成的交易紀錄
        self.equity_curve = []  # [(date, total_value)]

    def can_buy(self, sym, date):
        if sym in self.positions:
            return False
        if len(self.positions) >= self.max_positions:
            return False
        if sym in self.cooldown and date < self.cooldown[sym]:
            return False
        return True

    def buy(self, sym, price, date):
        if not self.can_buy(sym, date):
            return False
        target_value = self.initial_cash * self.position_size_pct
        target_value = min(target_value, self.cash * 0.95)  # 留 5% 安全
        if target_value < price:  # 買不到 1 股
            return False
        shares = target_value / price
        cost = shares * price * (1 + self.cost_pct)
        if cost > self.cash:
            return False
        self.cash -= cost
        self.positions[sym] = {
            'shares': shares,
            'entry_price': price,
            'entry_date': date,
            'entry_value': shares * price,
        }
        return True

    def sell(self, sym, price, date, reason=""):
        if sym not in self.positions:
            return False
        pos = self.positions[sym]
        proceeds = pos['shares'] * price * (1 - self.cost_pct)
        pnl = proceeds - pos['entry_value']
        pnl_pct = pnl / pos['entry_value']
        hold_days = (date - pos['entry_date']).days
        self.cash += proceeds
        self.trades.append({
            'symbol': sym,
            'entry_date': pos['entry_date'].strftime('%Y-%m-%d'),
            'entry_price': round(pos['entry_price'], 2),
            'exit_date': date.strftime('%Y-%m-%d'),
            'exit_price': round(price, 2),
            'shares': round(pos['shares'], 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct * 100, 2),
            'hold_days': hold_days,
            'reason': reason,
        })
        del self.positions[sym]
        self.cooldown[sym] = date + timedelta(days=self.cooldown_days)
        return True

    def total_value(self, date, get_price_fn):
        v = self.cash
        for sym, pos in self.positions.items():
            p = get_price_fn(sym, date)
            if p is None:
                p = pos['entry_price']
            v += pos['shares'] * p
        return v


# ======================================================
# 回測主引擎
# ======================================================
class BacktestEngine:
    def __init__(self, watchlist, start_date=None, end_date=None,
                 initial_cash=100_000, freq='W-MON',
                 buy_notify_min=4, sell_notify_min=8,
                 strong_buy_min=7, strong_sell_min=9):
        self.watchlist = watchlist
        # 預設回測過去 3 年
        if end_date is None:
            end_date = datetime.now().date()
        if start_date is None:
            start_date = end_date - timedelta(days=365 * 3)
        self.start_date = pd.Timestamp(start_date)
        self.end_date = pd.Timestamp(end_date)
        self.freq = freq

        self.buy_min = buy_notify_min
        self.sell_min = sell_notify_min
        self.strong_buy_min = strong_buy_min
        self.strong_sell_min = strong_sell_min

        self.loader = BacktestDataLoader(years=5)
        self.portfolio = BacktestPortfolio(initial_cash=initial_cash)

        # 複用 LongTermEngine 的純評估方法 (需要 instance 存取 self.drawdown_buy_pct 等)
        self.engine = LongTermEngine(watchlist=[])

    def _get_price_at(self, sym, date):
        close = self.loader.get_price_upto(sym, date)
        if close is None or close.empty:
            return None
        return float(close.iloc[-1])

    def _eval_signals(self, sym, date):
        close = self.loader.get_price_upto(sym, date)
        if close is None or len(close) < 60:
            return None
        market = self.loader.get_market_context_at(date)
        growth = self.loader.get_historical_growth(sym, date)
        buy_sigs = self.engine._evaluate_buy(close, market, growth)
        sell_sigs = self.engine._evaluate_sell(close, market, growth)
        return {
            'price': float(close.iloc[-1]),
            'buy_score': sum(w for _, w, _ in buy_sigs),
            'sell_score': sum(w for _, w, _ in sell_sigs),
            'buy_sigs': buy_sigs,
            'sell_sigs': sell_sigs,
            'growth_score': growth.get('growth_score_raw') if growth else None,
        }

    def run(self):
        print(f"🔬 回測開始: {self.start_date.date()} → {self.end_date.date()}")
        logger.info(f"========== 回測 {self.start_date.date()} → {self.end_date.date()} ==========")
        logger.info(f"Watchlist: {self.watchlist}")
        logger.info(f"初始資金: ${self.portfolio.initial_cash:,}")

        # 資料準備
        self.loader.prepare_all(self.watchlist)

        # 產生週評估日期
        dates = pd.date_range(self.start_date, self.end_date, freq=self.freq)
        print(f"  共 {len(dates)} 個評估時點 ({self.freq})")

        for date in tqdm(dates, desc="回測中", unit="週"):
            # 1. 評估所有標的訊號
            signals = {}
            for sym in self.watchlist:
                s = self._eval_signals(sym, date)
                if s:
                    signals[sym] = s

            # 2. 先處理賣出 (釋放資金)
            for sym, s in signals.items():
                if sym in self.portfolio.positions and s['sell_score'] >= self.sell_min:
                    reason = f"SellScore={s['sell_score']}: " + "+".join(n for n,_,_ in s['sell_sigs'][:3])
                    self.portfolio.sell(sym, s['price'], date, reason)
                    logger.info(f"[{date.date()}] SELL {sym} @${s['price']:.2f} | {reason}")

            # 3. 買入 (按 buy_score 降冪排序)
            buy_candidates = [(sym, s) for sym, s in signals.items()
                              if s['buy_score'] >= self.buy_min
                              and self.portfolio.can_buy(sym, date)]
            buy_candidates.sort(key=lambda x: x[1]['buy_score'], reverse=True)

            for sym, s in buy_candidates:
                if self.portfolio.buy(sym, s['price'], date):
                    reason = f"BuyScore={s['buy_score']}: " + "+".join(n for n,_,_ in s['buy_sigs'][:3])
                    logger.info(f"[{date.date()}] BUY  {sym} @${s['price']:.2f} | {reason}")

            # 4. 記錄淨值
            nv = self.portfolio.total_value(date, self._get_price_at)
            self.portfolio.equity_curve.append((date, nv))

        # 回測結束: 清倉以計算最終報酬
        end_date = dates[-1] if len(dates) > 0 else self.end_date
        for sym in list(self.portfolio.positions.keys()):
            price = self._get_price_at(sym, end_date)
            if price:
                self.portfolio.sell(sym, price, end_date, reason="回測結束清倉")

        self._generate_report()

    def _generate_report(self):
        trades = self.portfolio.trades
        equity = self.portfolio.equity_curve
        if not equity:
            print("⚠️ 無資料")
            return

        init_v = self.portfolio.initial_cash
        final_v = self.portfolio.cash  # 清倉後全為現金

        total_ret = (final_v - init_v) / init_v
        years = (self.end_date - self.start_date).days / 365
        annual_ret = ((final_v / init_v) ** (1/years) - 1) if years > 0 else 0

        # Drawdown
        values = np.array([v for _, v in equity])
        running_max = np.maximum.accumulate(values)
        drawdowns = (values - running_max) / running_max
        max_dd = drawdowns.min()

        # Sharpe (簡化, 週報酬)
        if len(values) > 1:
            returns = np.diff(values) / values[:-1]
            sharpe = np.sqrt(52) * returns.mean() / returns.std() if returns.std() > 0 else 0
        else:
            sharpe = 0

        # 勝率
        wins = [t for t in trades if t['pnl'] > 0]
        win_rate = len(wins) / len(trades) if trades else 0
        avg_hold = np.mean([t['hold_days'] for t in trades]) if trades else 0

        # SPY 基準
        spy_start = self.loader.spy_close.loc[:self.start_date]
        spy_end = self.loader.spy_close.loc[:self.end_date]
        if len(spy_start) > 0 and len(spy_end) > 0:
            spy_ret = (float(spy_end.iloc[-1]) - float(spy_start.iloc[-1])) / float(spy_start.iloc[-1])
        else:
            spy_ret = 0

        # === 終端報告 ===
        lines = []
        lines.append("\n" + "="*70)
        lines.append(f"📊 回測結果 ({self.start_date.date()} → {self.end_date.date()})")
        lines.append("="*70)
        lines.append(f"  初始資金:    ${init_v:>12,.0f}")
        lines.append(f"  結束資金:    ${final_v:>12,.0f}")
        lines.append(f"  總報酬率:    {total_ret*100:>+12.2f}%")
        lines.append(f"  年化報酬:    {annual_ret*100:>+12.2f}%")
        lines.append(f"  最大回撤:    {max_dd*100:>+12.2f}%")
        lines.append(f"  Sharpe:      {sharpe:>12.2f}")
        lines.append(f"  總交易數:    {len(trades):>12d}")
        lines.append(f"  勝率:        {win_rate*100:>12.1f}%")
        lines.append(f"  平均持有:    {avg_hold:>12.0f} 天")
        lines.append("")
        lines.append(f"  🏛️  買進持有 SPY: {spy_ret*100:+.2f}% (相同期間)")
        lines.append(f"  📈 超額報酬:     {(total_ret - spy_ret)*100:+.2f}%")
        lines.append("")

        # 個股 P&L
        lines.append("── 個股績效 ──")
        by_sym = {}
        for t in trades:
            by_sym.setdefault(t['symbol'], []).append(t)
        for sym in sorted(by_sym.keys()):
            sym_trades = by_sym[sym]
            total_pnl = sum(t['pnl'] for t in sym_trades)
            total_ret_pct = sum(t['pnl_pct'] for t in sym_trades)
            lines.append(f"  {sym:6s}  交易 {len(sym_trades):2d} 次  "
                        f"累積 P&L ${total_pnl:>+9,.0f}  "
                        f"累積報酬 {total_ret_pct:>+7.1f}%")

        lines.append("")
        lines.append("── 限制 ──")
        lines.append("  1. 存活偏差: 當前 watchlist，未考慮已下市")
        lines.append("  2. 基本面用年度數據近似，收益修正/獲利驚喜停用 (max 從 108 → 40)")
        lines.append("  3. 無滑價，以收盤價成交")
        lines.append("  4. 交易成本 0.1%/筆")
        lines.append("="*70)

        report = "\n".join(lines)
        print(report)
        logger.info(report)

        # 寫出交易紀錄 CSV
        if trades:
            csv_path = os.path.join(LOG_DIR, f"backtest_trades_{_ts}.csv")
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=trades[0].keys())
                writer.writeheader()
                writer.writerows(trades)
            print(f"💾 交易紀錄: {csv_path}")

        # 寫出 equity curve CSV
        eq_path = os.path.join(LOG_DIR, f"backtest_equity_{_ts}.csv")
        with open(eq_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['date', 'value'])
            for d, v in equity:
                writer.writerow([d.strftime('%Y-%m-%d'), round(v, 2)])
        print(f"💾 權益曲線: {eq_path}")


if __name__ == "__main__":
    raw = os.getenv("LT_WATCHLIST") or os.getenv("TIMING_WATCHLIST", "")
    watchlist = [s.strip() for s in raw.split(",") if s.strip()]
    if not watchlist:
        print("請在 .env 設定 TIMING_WATCHLIST")
    else:
        engine = BacktestEngine(watchlist=watchlist)
        engine.run()
