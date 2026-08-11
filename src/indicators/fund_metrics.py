"""基金指标工具：仅保留 UI 最大回撤图所需的回撤序列计算。"""

from __future__ import annotations

import pandas as pd


def build_drawdown_series(nav_df: pd.DataFrame, nav_col: str = "unit_nav") -> pd.DataFrame:
    """Return a dataframe with NAV and drawdown series for visualization.

    :param nav_col: 用于计算回撤的净值列（默认单位净值 unit_nav；复权口径可传 adjusted_nav）。
    """
    ordered = nav_df.sort_values("nav_date").reset_index(drop=True).copy()
    ordered[nav_col] = pd.to_numeric(ordered[nav_col], errors="coerce")
    ordered = ordered.dropna(subset=[nav_col])
    ordered["running_max"] = ordered[nav_col].cummax()
    ordered["drawdown_pct"] = (ordered[nav_col] / ordered["running_max"] - 1.0) * 100
    return ordered