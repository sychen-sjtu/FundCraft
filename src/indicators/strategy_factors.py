"""指数层日频策略因子计算：基于指数股息率/全收益价格 + cn_10y 利率，生成 index_daily_factors。

口径说明（对齐 gemini-code 文档与参考策略）：
- 股息率：底层指数股息率（index_valuation_history.dividend_yield，官方近20日 + 推导历史前缀，
  官方优先合并），按日期前向填充对齐；指数估值由每次刷新增量入库累积。
- 年化波动率：**全收益指数价格**日收益 252 天滚动年化（除息平滑）
- 最大回撤：**全收益指数价格**截至当日的滚动最大回撤（0~负值）
- 历史分位：对每个因子做「截至当日的扩展窗口百分位」(0~100)，样本起点默认 2013-12-31
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

VOL_WINDOW_DAYS = 252
TRADING_DAYS = 252
SCORE_THRESHOLD = 80.0
# 分位窗口样本起点（gemini 文档口径：2013-12-31 起算全量历史）
SAMPLE_START = pd.Timestamp("2013-12-31")

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


def compute_index_factors(
    index_code: str,
    dividend_yield_df: pd.DataFrame,
    price_df: pd.DataFrame,
    rate_df: pd.DataFrame,
    *,
    sample_start: str | pd.Timestamp | None = SAMPLE_START,
) -> pd.DataFrame:
    """计算指数层日频策略因子（index_daily_factors，ER 范式：因子绑定指数，多基金共用信号）。

    口径（对齐 index_daily_factors 定义）：
    - 股息率：index_valuation_history（官方近20日 + 推导历史前缀，官方优先合并）
    - 年化波动率 / 最大回撤：取【全收益指数】价格（除息平滑）
    - 分位样本起点：sample_start（默认 2013-12-31）
    :return: index_daily_factors 所需的 DataFrame（index_code/trade_date/...）
    """
    if price_df is None or price_df.empty:
        return pd.DataFrame()
    if dividend_yield_df is None or dividend_yield_df.empty:
        raise ValueError("缺少指数股息率数据（index_valuation_history），无法计算策略得分。")
    if rate_df is None or rate_df.empty:
        raise ValueError("缺少 cn_10y 利率数据（macro_rates_history），无法计算利差。")

    base = price_df[["trade_date", "close"]].rename(columns={"close": "unit_nav"}).copy()
    base["trade_date"] = pd.to_datetime(base["trade_date"], errors="coerce")
    base["unit_nav"] = pd.to_numeric(base["unit_nav"], errors="coerce")
    base = (
        base.dropna(subset=["trade_date", "unit_nav"])
        .drop_duplicates("trade_date")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    if sample_start is not None:
        base = base[base["trade_date"] >= pd.Timestamp(sample_start)].reset_index(drop=True)
    if base.empty:
        return pd.DataFrame()

    # 股息率（官方+推导，前向填充对齐）
    dy = dividend_yield_df[["trade_date", "dividend_yield"]].copy()
    dy["trade_date"] = pd.to_datetime(dy["trade_date"], errors="coerce")
    dy["dividend_yield"] = pd.to_numeric(dy["dividend_yield"], errors="coerce")
    dy = dy.dropna(subset=["trade_date", "dividend_yield"]).sort_values("trade_date").drop_duplicates("trade_date")
    base = base.merge(dy, on="trade_date", how="left")
    base["dividend_yield"] = base["dividend_yield"].ffill()

    # 年化波动率 / 最大回撤（全收益指数价格）
    daily_return = base["unit_nav"].pct_change()
    base["annualized_volatility"] = daily_return.rolling(VOL_WINDOW_DAYS).std(ddof=1) * np.sqrt(TRADING_DAYS) * 100.0
    base["max_drawdown"] = _compute_max_drawdown(base["unit_nav"])

    # 利差 = 股息率 - cn_10y
    rate = rate_df[["trade_date", "rate_value"]].copy()
    rate["trade_date"] = pd.to_datetime(rate["trade_date"], errors="coerce")
    rate["rate_value"] = pd.to_numeric(rate["rate_value"], errors="coerce")
    rate = rate.dropna(subset=["trade_date", "rate_value"]).sort_values("trade_date").drop_duplicates("trade_date")
    base = base.merge(rate, on="trade_date", how="left")
    base["cn_10y"] = base["rate_value"].ffill()
    base["spread"] = base["dividend_yield"] - base["cn_10y"]
    base["dy_vol_ratio"] = base["dividend_yield"] / base["annualized_volatility"]

    # 历史分位（扩展窗口）
    base["dividend_yield_percentile"] = _expanding_percentile(base["dividend_yield"])
    base["spread_percentile"] = _expanding_percentile(base["spread"])
    base["dy_vol_ratio_percentile"] = _expanding_percentile(base["dy_vol_ratio"])
    base["drawdown_percentile"] = _expanding_percentile(base["max_drawdown"], invert=True)
    base["volatility_percentile"] = _expanding_percentile(base["annualized_volatility"])

    a_weights = STRATEGY_A_WEIGHTS
    base["score_a"] = (
        base["spread_percentile"] * a_weights["spread"]
        + base["drawdown_percentile"] * a_weights["drawdown"]
        + base["volatility_percentile"] * a_weights["vol"]
    )
    b_weights = STRATEGY_B_WEIGHTS
    base["score_b"] = (
        base["dividend_yield_percentile"] * b_weights["dividend_yield"]
        + base["spread_percentile"] * b_weights["spread"]
        + base["dy_vol_ratio_percentile"] * b_weights["dy_vol_ratio"]
        + base["drawdown_percentile"] * b_weights["drawdown"]
        + base["volatility_percentile"] * b_weights["vol"]
    )
    base["signal_a"] = base["score_a"] >= SCORE_THRESHOLD
    base["signal_b"] = base["score_b"] >= SCORE_THRESHOLD
    base["index_code"] = index_code

    columns = [
        "index_code", "trade_date", "dividend_yield", "annualized_volatility", "max_drawdown",
        "dividend_yield_percentile", "spread", "spread_percentile", "dy_vol_ratio_percentile",
        "drawdown_percentile", "volatility_percentile", "score_a", "signal_a", "score_b", "signal_b",
    ]
    factors = base[columns].copy()
    factors = factors.dropna(subset=["score_a", "score_b"])
    return factors.reset_index(drop=True)
