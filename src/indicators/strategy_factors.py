"""策略日频因子计算：基于底层指数价格/股息率 + cn_10y 利率，生成 fund_daily_factors。

口径说明（口径 C，对齐 gemini-code 文档与参考策略）：
- 股息率：采用该基金对应**底层指数的股息率**（index_valuation_history.dividend_yield1），
  按日期前向填充对齐；指数估值由每次刷新增量入库累积。
- 年化波动率：**底层指数价格**日收益 252 天滚动年化（传入 index_price_df 时取指数，否则回退基金净值）
- 最大回撤：**底层指数价格**截至当日的滚动最大回撤（0~负值）
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


def compute_fund_factors(
    nav_df: pd.DataFrame,
    index_val_df: pd.DataFrame,
    rate_df: pd.DataFrame,
    *,
    index_price_df: pd.DataFrame | None = None,
    sample_start: str | pd.Timestamp | None = SAMPLE_START,
) -> pd.DataFrame:
    """计算单只基金的完整日频因子表（口径 C：波动/回撤取指数价格）。

    :param nav_df: 基金净值（fund_code/nav_date/unit_nav），用于取基金代码与回退口径。
    :param index_val_df: 底层指数估值（index_code/trade_date/dividend_yield1），
        由 index_valuation_history 入库累积（官方）+ 推导历史合并。
    :param rate_df: cn_10y 利率（rate_code/rate_date/rate_value）。
    :param index_price_df: 底层指数价格（trade_date/close）。提供时，年化波动率与
        最大回撤取【指数价格】（口径 C，与参考策略一致）；缺省回退取基金净值。
    :param sample_start: 分位窗口样本起点（默认 2013-12-31）；None 表示用数据起点。
    :return: fund_daily_factors 所需的 DataFrame（可按 fund_code/trade_date 入库）。
    """
    # 基金代码（兼容：从净值表取；缺省为空字符串）
    fund_code = ""
    if nav_df is not None and not nav_df.empty:
        fund_code = str(nav_df["fund_code"].iloc[0])

    # 基础价格序列：指数价格优先（口径 C），否则回退基金净值
    if index_price_df is not None and not index_price_df.empty:
        base = index_price_df[["trade_date", "close"]].rename(columns={"close": "unit_nav"}).copy()
    else:
        base = nav_df[["nav_date", "unit_nav"]].rename(columns={"nav_date": "trade_date"}).copy()
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

    # 1) 股息率：采用底层指数股息率（dividend_yield1），按日期前向填充对齐
    index_val = index_val_df[["trade_date", "dividend_yield1"]].copy()
    index_val["trade_date"] = pd.to_datetime(index_val["trade_date"], errors="coerce")
    index_val["dividend_yield1"] = pd.to_numeric(index_val["dividend_yield1"], errors="coerce")
    index_val = index_val.dropna(subset=["trade_date", "dividend_yield1"]).sort_values("trade_date")
    if index_val.empty:
        raise ValueError("缺少底层指数估值数据（股息率），无法计算策略得分。请先同步 index_valuation_history。")
    base = base.merge(
        index_val.rename(columns={"dividend_yield1": "dividend_yield"}),
        on="trade_date",
        how="left",
    )
    base["dividend_yield"] = base["dividend_yield"].ffill()

    # 2) 年化波动率（基础价格日收益 252 天滚动）
    daily_return = base["unit_nav"].pct_change()
    base["annualized_vol"] = daily_return.rolling(VOL_WINDOW_DAYS).std(ddof=1) * np.sqrt(TRADING_DAYS) * 100.0

    # 3) 最大回撤（基础价格截至当日的滚动回撤）
    base["max_drawdown"] = _compute_max_drawdown(base["unit_nav"])

    # 4) 利差 = 指数股息率 - cn_10y（利率按日期前向填充对齐）
    rate = rate_df[["rate_date", "rate_value"]].copy()
    rate["rate_date"] = pd.to_datetime(rate["rate_date"], errors="coerce")
    rate = rate.dropna(subset=["rate_date", "rate_value"]).sort_values("rate_date")
    if rate.empty:
        raise ValueError("缺少 cn_10y 利率数据，无法计算利差与策略得分。请先同步 macro_rates_history。")
    base = base.merge(
        rate.rename(columns={"rate_date": "trade_date", "rate_value": "cn_10y"}),
        on="trade_date",
        how="left",
    )
    base["cn_10y"] = base["cn_10y"].ffill()

    base["spread"] = base["dividend_yield"] - base["cn_10y"]
    base["dy_vol_ratio"] = base["dividend_yield"] / base["annualized_vol"]

    # 5) 历史分位（扩展窗口，样本起点 sample_start）
    base["dividend_yield_pctile"] = _expanding_percentile(base["dividend_yield"])
    base["spread_pctile"] = _expanding_percentile(base["spread"])
    base["dy_vol_ratio_pctile"] = _expanding_percentile(base["dy_vol_ratio"])
    base["drawdown_pctile"] = _expanding_percentile(base["max_drawdown"], invert=True)
    base["vol_pctile"] = _expanding_percentile(base["annualized_vol"])

    # 6) 策略得分
    a_weights = STRATEGY_A_WEIGHTS
    base["score_a"] = (
        base["spread_pctile"] * a_weights["spread"]
        + base["drawdown_pctile"] * a_weights["drawdown"]
        + base["vol_pctile"] * a_weights["vol"]
    )
    b_weights = STRATEGY_B_WEIGHTS
    base["score_b"] = (
        base["dividend_yield_pctile"] * b_weights["dividend_yield"]
        + base["spread_pctile"] * b_weights["spread"]
        + base["dy_vol_ratio_pctile"] * b_weights["dy_vol_ratio"]
        + base["drawdown_pctile"] * b_weights["drawdown"]
        + base["vol_pctile"] * b_weights["vol"]
    )
    base["signal_a"] = base["score_a"] >= SCORE_THRESHOLD
    base["signal_b"] = base["score_b"] >= SCORE_THRESHOLD
    base["fund_code"] = fund_code

    columns = [
        "fund_code",
        "trade_date",
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
    factors = base[columns].copy()

    # 分位/得分在扩展窗口不足时可能为 NaN，跳过（保证入库字段非空）
    factors = factors.dropna(subset=["score_a", "score_b"])
    return factors.reset_index(drop=True)
