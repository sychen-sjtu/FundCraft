from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class FundPerformanceMetrics:
    fund_code: str
    start_date: str
    end_date: str
    row_count: int
    start_unit_nav: float
    end_unit_nav: float
    cumulative_return_pct: float
    max_drawdown_pct: float
    annualized_volatility_pct: float


def compute_fund_metrics(nav_df: pd.DataFrame) -> FundPerformanceMetrics:
    """Compute basic fund performance metrics from a normalized NAV dataframe."""
    required_columns = {"fund_code", "nav_date", "unit_nav"}
    missing_columns = required_columns - set(nav_df.columns)
    if missing_columns:
        raise ValueError(f"Missing required columns: {sorted(missing_columns)}")

    if nav_df.empty:
        raise ValueError("NAV dataframe is empty")

    ordered = nav_df.sort_values("nav_date").reset_index(drop=True).copy()
    ordered["unit_nav"] = pd.to_numeric(ordered["unit_nav"], errors="coerce")
    ordered = ordered.dropna(subset=["unit_nav"])
    if ordered.empty:
        raise ValueError("NAV dataframe has no valid unit_nav values")

    fund_code = str(ordered["fund_code"].iloc[0]).strip()
    if fund_code.isdigit() and len(fund_code) < 6:
        fund_code = fund_code.zfill(6)
    start_nav = float(ordered["unit_nav"].iloc[0])
    end_nav = float(ordered["unit_nav"].iloc[-1])
    nav_series = ordered["unit_nav"]
    daily_returns = nav_series.pct_change().dropna()

    cumulative_return_pct = (end_nav / start_nav - 1.0) * 100 if start_nav != 0 else np.nan
    running_max = nav_series.cummax()
    drawdown = nav_series / running_max - 1.0
    max_drawdown_pct = float(drawdown.min() * 100)
    annualized_volatility_pct = float(daily_returns.std(ddof=1) * np.sqrt(252) * 100) if len(daily_returns) > 1 else 0.0

    return FundPerformanceMetrics(
        fund_code=fund_code,
        start_date=ordered["nav_date"].min().date().isoformat(),
        end_date=ordered["nav_date"].max().date().isoformat(),
        row_count=int(len(ordered)),
        start_unit_nav=start_nav,
        end_unit_nav=end_nav,
        cumulative_return_pct=float(cumulative_return_pct),
        max_drawdown_pct=max_drawdown_pct,
        annualized_volatility_pct=annualized_volatility_pct,
    )


def build_drawdown_series(nav_df: pd.DataFrame) -> pd.DataFrame:
    """Return a dataframe with NAV and drawdown series for visualization."""
    ordered = nav_df.sort_values("nav_date").reset_index(drop=True).copy()
    ordered["unit_nav"] = pd.to_numeric(ordered["unit_nav"], errors="coerce")
    ordered = ordered.dropna(subset=["unit_nav"])
    ordered["running_max"] = ordered["unit_nav"].cummax()
    ordered["drawdown_pct"] = (ordered["unit_nav"] / ordered["running_max"] - 1.0) * 100
    return ordered