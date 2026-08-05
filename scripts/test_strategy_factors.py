"""临时验证脚本：验证分红抓取 + 利率抓取 + 股息率合成 + 策略因子计算。"""
from __future__ import annotations

import sys
from pathlib import Path

import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.fetchers.fund_dividend_fetcher import fetch_fund_dividends
from src.fetchers.macro_fetcher import fetch_cn_10y_rate
from src.indicators.dividend_yield import compute_dividend_yield_series
from src.indicators.strategy_factors import compute_fund_factors
from src.storage.local_store import find_latest_raw_snapshot, load_fund_snapshots

warnings.filterwarnings("ignore")

# 1) 分红抓取
div = fetch_fund_dividends(["008163"])
print("===== 008163 分红 =====")
print(f"shape={div.shape}")
print(div.tail(5).to_string())

# 2) 利率抓取
rate = fetch_cn_10y_rate()
print("\n===== cn_10y 利率 =====")
print(f"shape={rate.shape} range={rate['rate_date'].min()} ~ {rate['rate_date'].max()}")
print(rate.tail(3).to_string())

# 3) 从本地快照读取 008163 净值
from pathlib import Path

snapshot_dir = find_latest_raw_snapshot(Path.cwd())
snapshots = load_fund_snapshots(snapshot_dir)
nav = next((df for df in snapshots if str(df["fund_code"].iloc[0]) == "008163"), None)
if nav is None:
    raise SystemExit("本地快照中未找到 008163 净值，请先运行抓取。")
print(f"\n===== 008163 净值 =====")
print(f"shape={nav.shape} range={nav['nav_date'].min()} ~ {nav['nav_date'].max()}")

# 4) 股息率合成
dy = compute_dividend_yield_series(nav, div)
print("\n===== 合成股息率（最近5日）=====")
print(dy.tail(5).to_string())

# 5) 策略因子
factors = compute_fund_factors(nav, div, rate)
print("\n===== 策略因子（最近5日）=====")
cols = ["trade_date", "dividend_yield", "annualized_vol", "max_drawdown", "dividend_yield_pctile",
        "spread", "spread_pctile", "dy_vol_ratio_pctile", "drawdown_pctile", "vol_pctile",
        "score_a", "signal_a", "score_b", "signal_b"]
print(f"shape={factors.shape}")
print(factors[cols].tail(5).to_string())

# 6) 信号统计
if not factors.empty:
    hits_a = int(factors["signal_a"].sum())
    hits_b = int(factors["signal_b"].sum())
    print(f"\n触发买入信号天数：A={hits_a}, B={hits_b} (总 {len(factors)} 天)")
