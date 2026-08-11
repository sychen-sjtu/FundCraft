"""基金指标工具：仅保留 UI 最大回撤图所需的回撤序列计算。"""

from __future__ import annotations

import pandas as pd


def build_drawdown_series(nav_df: pd.DataFrame) -> pd.DataFrame:
    """Return a dataframe with NAV and drawdown series for visualization."""
    ordered = nav_df.sort_values("nav_date").reset_index(drop=True).copy()
    ordered["unit_nav"] = pd.to_numeric(ordered["unit_nav"], errors="coerce")
    ordered = ordered.dropna(subset=["unit_nav"])
    ordered["running_max"] = ordered["unit_nav"].cummax()
    ordered["drawdown_pct"] = (ordered["unit_nav"] / ordered["running_max"] - 1.0) * 100
    return ordered