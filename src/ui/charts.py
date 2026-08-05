"""图表构建：dashboard 与 各类别面板 共用的 plotly 图表函数。"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.indicators.fund_metrics import build_drawdown_series

COLORS = ["#2563EB", "#16A34A", "#F97316", "#8B5CF6", "#EF4444"]


def build_overview_chart(summary_df: pd.DataFrame) -> go.Figure:
    figure = go.Figure()
    if summary_df.empty:
        return figure

    ordered = summary_df.sort_values("cumulative_return_pct", ascending=False)
    figure.add_trace(
        go.Bar(
            x=ordered["fund_code"],
            y=ordered["cumulative_return_pct"],
            name="累计收益率",
            marker_color="#2563EB",
        )
    )
    figure.update_layout(
        template="plotly_white",
        title="基金累计收益率对比",
        yaxis_title="累计收益率 (%)",
        xaxis_title="基金代码",
        height=420,
        margin=dict(l=30, r=20, t=60, b=40),
    )
    return figure


def build_nav_and_drawdown_chart(nav_df: pd.DataFrame) -> go.Figure:
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.68, 0.32],
        subplot_titles=("单位净值走势", "回撤走势 (%)"),
    )

    if nav_df.empty:
        return figure

    for index, fund_code in enumerate(sorted(nav_df["fund_code"].astype(str).unique().tolist())):
        fund_nav = nav_df[nav_df["fund_code"] == fund_code].sort_values("nav_date")
        drawdown_df = build_drawdown_series(fund_nav)
        color = COLORS[index % len(COLORS)]

        figure.add_trace(
            go.Scatter(
                x=fund_nav["nav_date"],
                y=fund_nav["unit_nav"],
                mode="lines",
                name=f"{fund_code} NAV",
                line=dict(color=color, width=2),
            ),
            row=1,
            col=1,
        )
        figure.add_trace(
            go.Scatter(
                x=drawdown_df["nav_date"],
                y=drawdown_df["drawdown_pct"],
                mode="lines",
                name=f"{fund_code} Drawdown",
                line=dict(color=color, width=2, dash="dot"),
                showlegend=False,
            ),
            row=2,
            col=1,
        )

    figure.update_layout(
        template="plotly_white",
        height=760,
        margin=dict(l=30, r=20, t=70, b=40),
        legend_title_text="基金代码 / 指标",
    )
    figure.update_yaxes(title_text="单位净值", row=1, col=1)
    figure.update_yaxes(title_text="回撤 (%)", row=2, col=1)
    return figure


def build_dividend_yield_chart(nav_df: pd.DataFrame, dividend_df: pd.DataFrame) -> go.Figure:
    """合成股息率走势图（含分红除息日标记）。"""
    from src.indicators.dividend_yield import compute_dividend_yield_series

    figure = go.Figure()
    dy_df = compute_dividend_yield_series(nav_df, dividend_df)
    if dy_df.empty:
        return figure

    figure.add_trace(
        go.Scatter(
            x=dy_df["nav_date"],
            y=dy_df["dividend_yield"],
            mode="lines",
            name="合成股息率 (%)",
            line=dict(color="#2563EB", width=2),
        )
    )

    if not dividend_df.empty:
        ex_dates = pd.to_datetime(dividend_df["ex_date"], errors="coerce").dropna()
        figure.add_trace(
            go.Scatter(
                x=ex_dates,
                y=[None] * len(ex_dates),
                mode="markers",
                name="除息日",
                marker=dict(color="#EF4444", size=6, symbol="triangle-down"),
                hoverinfo="skip",
            )
        )

    figure.update_layout(
        template="plotly_white",
        title="合成股息率（近 365 天分红 ÷ 单位净值）",
        yaxis_title="股息率 (%)",
        xaxis_title="日期",
        height=360,
        margin=dict(l=30, r=20, t=60, b=40),
        legend_title_text="指标",
    )
    return figure
