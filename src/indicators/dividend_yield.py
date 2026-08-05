"""合成股息率：用「基金分红 + 基金净值」合成日频股息率序列。

口径：股息率(t) = 过去 window_days 天内累计每份分红 / 当日单位净值 × 100%
分红按「除息日」归属到对应日期，非分红日向前填充（滚动窗口内累计值不变）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_WINDOW_DAYS = 365


def compute_dividend_yield_series(
    nav_df: pd.DataFrame,
    dividend_df: pd.DataFrame,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> pd.DataFrame:
    """按日频合成股息率。

    :param nav_df: 净值 DataFrame，需含 fund_code / nav_date / unit_nav。
    :param dividend_df: 分红 DataFrame，需含 fund_code / ex_date / dividend_per_unit。
    :param window_days: 滚动窗口天数（默认 365）。
    :return: DataFrame，含 fund_code / nav_date / dividend_yield(%)
    """
    nav = nav_df[["fund_code", "nav_date", "unit_nav"]].copy()
    nav["nav_date"] = pd.to_datetime(nav["nav_date"], errors="coerce")
    nav["unit_nav"] = pd.to_numeric(nav["unit_nav"], errors="coerce")
    nav = nav.dropna(subset=["nav_date", "unit_nav"]).sort_values("nav_date").reset_index(drop=True)

    if nav.empty:
        return nav[["fund_code", "nav_date", "dividend_yield"]] if "dividend_yield" in nav else pd.DataFrame(
            columns=["fund_code", "nav_date", "dividend_yield"]
        )

    div = dividend_df[["ex_date", "dividend_per_unit"]].copy()
    div["ex_date"] = pd.to_datetime(div["ex_date"], errors="coerce")
    div["dividend_per_unit"] = pd.to_numeric(div["dividend_per_unit"], errors="coerce")
    div = div.dropna(subset=["ex_date", "dividend_per_unit"]).sort_values("ex_date").reset_index(drop=True)

    if div.empty:
        nav["dividend_yield"] = 0.0
        return nav[["fund_code", "nav_date", "dividend_yield"]]

    ex_dates = div["ex_date"].to_numpy()
    amounts = div["dividend_per_unit"].to_numpy(dtype=float)
    cumsum = np.concatenate([[0.0], np.cumsum(amounts)])

    nav_dates = nav["nav_date"].to_numpy()
    idx_now = np.searchsorted(ex_dates, nav_dates, side="right")  # ex_date <= t 的数量
    idx_prev = np.searchsorted(ex_dates, nav_dates - np.timedelta64(window_days, "D"), side="right")

    trailing_sum = cumsum[idx_now] - cumsum[idx_prev]
    nav["dividend_yield"] = trailing_sum / nav["unit_nav"].to_numpy() * 100.0

    return nav[["fund_code", "nav_date", "dividend_yield"]]
