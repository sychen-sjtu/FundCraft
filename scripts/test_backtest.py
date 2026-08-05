"""临时验证：本地运行 A/B 策略回测。"""
from __future__ import annotations

import sys
from pathlib import Path

import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.fetchers.fund_dividend_fetcher import fetch_fund_dividends  # noqa: E402
from src.fetchers.macro_fetcher import fetch_cn_10y_rate  # noqa: E402
from src.indicators.strategy_backtest import run_backtest  # noqa: E402
from src.indicators.strategy_factors import compute_fund_factors  # noqa: E402
from src.storage.local_store import find_latest_raw_snapshot, load_fund_snapshots  # noqa: E402

warnings.filterwarnings("ignore")

snapshot_dir = find_latest_raw_snapshot(Path.cwd())
snapshots = load_fund_snapshots(snapshot_dir)
nav = next((df for df in snapshots if str(df["fund_code"].iloc[0]) == "008163"), None)
if nav is None:
    raise SystemExit("本地快照中未找到 008163 净值")

div = fetch_fund_dividends(["008163"])
rate = fetch_cn_10y_rate()
factors = compute_fund_factors(nav, div, rate)

print(f"因子区间: {factors['trade_date'].min().date()} ~ {factors['trade_date'].max().date()} ({len(factors)} 行)")

for score_col in ["score_a", "score_b"]:
    result = run_backtest(factors, nav, rate, score_column=score_col)
    print(f"\n===== 策略 {score_col.upper()} =====")
    print(f"XIRR          : {result['xirr_pct']:.2f}%")
    print(f"最大回撤      : {result['max_drawdown_pct']:.2f}%")
    print(f"买入次数      : {result['num_buys']}")
    print(f"总投入        : {result['total_invested']:.0f} 元")
    print(f"期末组合价值  : {result['final_value']:.2f} 元")
    print(f"回测区间      : {result['start_date']} ~ {result['end_date']}")
