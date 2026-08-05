"""策略日频因子计算：基于 008163 净值 + 分红 + cn_10y 利率，生成 fund_daily_factors。

口径说明（对应 docs/数据持久化与增量同步设计方案.md §5）：
- 合成股息率：见 dividend_yield.compute_dividend_yield_series
- 年化波动率：日收益 252 天滚动年化
- 最大回撤：截至当日的滚动最大回撤（0~负值）
- 历史分位：对每个因子做「截至当日的扩展窗口百分位」(0~100)
  - 股息率/利差/股息率-波动率比：值越高 → 分位越高
  - 最大回撤：越深（越负）→ 分位越高（买入信号更积极）
- 策略得分（A/B，见 gemini-code 文档）：
  - A = 利差分位×70% + 回撤分位×12.5% + 波动率分位×17.5%
  - B = 股息率分位×5% + 利差分位×60% + 股息率/波动率分位×10% + 回撤分位×20% + 波动率分位×5%
  - 得分 ≥ 80 → 触发买入信号
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators.dividend_yield import compute_dividend_yield_series

VOL_WINDOW_DAYS = 252
TRADING_DAYS = 252
SCORE_THRESHOLD = 80.0

# A / B 策略权重（与 gemini-code 文档一致）
STRATEGY_A_WEIGHTS = {"spread": 0.70, "drawdown": 0.125, "vol": 0.175}
STRATEGY_B_WEIGHTS = {
    "dividend_yield": 0.05,
    "spread": 0.60,
    "dy_vol_ratio": 0.10,
    "drawdown": 0.20,
    "vol": 0.05,
}


def _expanding_percentile(series: pd.Series, *, invert: bool = False) -> pd.Series:
    """计算截至每个时点的扩展窗口百分位（0~100）。

    :param series: 数值序列（按时间升序）。
    :param invert: 为 True 时反转（最小值 → 100），用于"回撤越深分位越高"。
    """
    values = series.to_numpy(dtype=float)
    n = len(values)
    result = np.full(n, np.nan, dtype=float)

    for i in range(n):
        window = values[: i + 1]
        window = window[~np.isnan(window)]
        if len(window) < 2:
            result[i] = np.nan
            continue
        # 当前值在窗口内的百分位（含当前值）
        rank = (window <= values[i]).sum()
        pct = (rank - 1) / (len(window) - 1) * 100.0 if len(window) > 1 else 100.0
        result[i] = 100.0 - pct if invert else pct

    return pd.Series(result, index=series.index)


def _compute_max_drawdown(unit_nav: pd.Series) -> pd.Series:
    running_max = unit_nav.cummax()
    drawdown = unit_nav / running_max - 1.0
    return drawdown * 100.0


def compute_fund_factors(
    nav_df: pd.DataFrame,
    dividend_df: pd.DataFrame,
    rate_df: pd.DataFrame,
) -> pd.DataFrame:
    """计算单只基金的完整日频因子表。

    :param nav_df: 净值（fund_code/nav_date/unit_nav），应为全历史。
    :param dividend_df: 分红（fund_code/ex_date/dividend_per_unit）。
    :param rate_df: cn_10y 利率（rate_code/rate_date/rate_value）。
    :return: fund_daily_factors 所需的 DataFrame（可按 fund_code/trade_date 入库）。
    """
    nav = nav_df[["fund_code", "nav_date", "unit_nav"]].copy()
    nav["nav_date"] = pd.to_datetime(nav["nav_date"], errors="coerce")
    nav["unit_nav"] = pd.to_numeric(nav["unit_nav"], errors="coerce")
    nav = nav.dropna(subset=["nav_date", "unit_nav"]).sort_values("nav_date").reset_index(drop=True)
    if nav.empty:
        return pd.DataFrame()

    fund_code = str(nav["fund_code"].iloc[0])

    # 1) 合成股息率
    dy_df = compute_dividend_yield_series(nav, dividend_df)
    nav = nav.merge(dy_df[["nav_date", "dividend_yield"]], on="nav_date", how="left")

    # 2) 年化波动率（日收益 252 天滚动）
    daily_return = nav["unit_nav"].pct_change()
    annualized_vol = daily_return.rolling(VOL_WINDOW_DAYS).std(ddof=1) * np.sqrt(TRADING_DAYS) * 100.0

    # 3) 最大回撤（截至当日的滚动回撤）
    max_drawdown = _compute_max_drawdown(nav["unit_nav"])

    # 4) 利差 = 合成股息率 - cn_10y（利率按日期前向填充对齐）
    rate = rate_df[["rate_date", "rate_value"]].copy()
    rate["rate_date"] = pd.to_datetime(rate["rate_date"], errors="coerce")
    rate = rate.dropna(subset=["rate_date", "rate_value"]).sort_values("rate_date")
    nav = nav.merge(
        rate.rename(columns={"rate_date": "nav_date", "rate_value": "cn_10y"}),
        on="nav_date",
        how="left",
    )
    nav["cn_10y"] = nav["cn_10y"].ffill()

    nav["annualized_vol"] = annualized_vol
    nav["max_drawdown"] = max_drawdown
    nav["spread"] = nav["dividend_yield"] - nav["cn_10y"]
    nav["dy_vol_ratio"] = nav["dividend_yield"] / nav["annualized_vol"]

    # 5) 历史分位（扩展窗口）
    nav["dividend_yield_pctile"] = _expanding_percentile(nav["dividend_yield"])
    nav["spread_pctile"] = _expanding_percentile(nav["spread"])
    nav["dy_vol_ratio_pctile"] = _expanding_percentile(nav["dy_vol_ratio"])
    nav["drawdown_pctile"] = _expanding_percentile(nav["max_drawdown"], invert=True)
    nav["vol_pctile"] = _expanding_percentile(nav["annualized_vol"])

    # 6) 策略得分
    a_weights = STRATEGY_A_WEIGHTS
    nav["score_a"] = (
        nav["spread_pctile"] * a_weights["spread"]
        + nav["drawdown_pctile"] * a_weights["drawdown"]
        + nav["vol_pctile"] * a_weights["vol"]
    )
    b_weights = STRATEGY_B_WEIGHTS
    nav["score_b"] = (
        nav["dividend_yield_pctile"] * b_weights["dividend_yield"]
        + nav["spread_pctile"] * b_weights["spread"]
        + nav["dy_vol_ratio_pctile"] * b_weights["dy_vol_ratio"]
        + nav["drawdown_pctile"] * b_weights["drawdown"]
        + nav["vol_pctile"] * b_weights["vol"]
    )
    nav["signal_a"] = nav["score_a"] >= SCORE_THRESHOLD
    nav["signal_b"] = nav["score_b"] >= SCORE_THRESHOLD

    columns = [
        "fund_code",
        "nav_date",
        "dividend_yield",
        "annualized_vol",
        "max_drawdown",
        "dividend_yield_pctile",
        "spread",
        "spread_pctile",
        "dy_vol_ratio_pctile",
        "drawdown_pctile",
        "vol_pctile",
        "score_a",
        "signal_a",
        "score_b",
        "signal_b",
    ]
    factors = nav[columns].rename(columns={"nav_date": "trade_date"}).copy()

    # 分位/得分在扩展窗口不足时可能为 NaN，跳过（保证入库字段非空）
    factors = factors.dropna(subset=["score_a", "score_b"])
    factors["fund_code"] = fund_code
    return factors.reset_index(drop=True)
