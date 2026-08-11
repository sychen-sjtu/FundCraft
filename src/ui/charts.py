"""图表构建：FundCraft 各页面共用的 plotly 图表函数。"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from src.indicators.fund_metrics import build_drawdown_series


def _hex_with_alpha(hex_color: str, alpha: float) -> str:
    """把 #RRGGBB 转成带透明度的 rgba() 字符串（Plotly 不接受 8 位十六进制）。"""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _series_direction(nav_df: pd.DataFrame) -> int:
    """区间整体方向：1 上涨 / -1 下跌 / 0 平（按首尾净值判断）。"""
    if nav_df.empty or len(nav_df) < 2:
        return 0
    first = float(nav_df["unit_nav"].iloc[0])
    last = float(nav_df["unit_nav"].iloc[-1])
    if last > first:
        return 1
    if last < first:
        return -1
    return 0


def build_nav_area_chart(nav_df: pd.DataFrame) -> go.Figure:
    """单位净值面积图（支付宝风格）：区间上涨红色填充、下跌绿色填充。"""
    from src.ui.theme import COLOR_DOWN, COLOR_UP

    figure = go.Figure()
    if nav_df.empty:
        return figure

    ordered = nav_df.sort_values("nav_date")
    direction = _series_direction(ordered)
    fill_color = COLOR_UP if direction >= 0 else COLOR_DOWN

    figure.add_trace(
        go.Scatter(
            x=ordered["nav_date"],
            y=ordered["unit_nav"],
            mode="lines",
            name="单位净值",
            line=dict(color=fill_color, width=2.2),
            fill="tozeroy",
            fillcolor=_hex_with_alpha(fill_color, 0.10),
            hovertemplate="%{x|%Y-%m-%d}<br>单位净值：%{y:.4f}<extra></extra>",
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=240,
        margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
        dragmode=False,
        xaxis_title="",
        yaxis_title="单位净值",
    )
    figure.update_xaxes(showgrid=True, gridcolor="#F0F1F3", tickformat="%Y-%m-%d")
    figure.update_yaxes(showgrid=True, gridcolor="#F0F1F3")
    return figure


def build_cumulative_vs_benchmark_chart(
    nav_df: pd.DataFrame,
    benchmark_df: pd.DataFrame,
    benchmark_name: str = "沪深300",
    show_legend: bool = True,
) -> go.Figure:
    """累计收益率 vs 大盘指数对比（区间首日归一化为 0%）。

    主基金高亮橙（COLOR_FUND_HIGHLIGHT），对比指数中性灰细虚线（COLOR_BENCHMARK），
    去填充、轻网格，聚焦主线。

    :param benchmark_name: 对比指数显示名（图例/hover，如 沪深300 / 上证指数 / 深证成指）。
    :param show_legend: 是否显示 Plotly 图例（页面内用图例统计条代替时传 False）。
    """
    from src.ui.theme import COLOR_BENCHMARK, COLOR_FUND_HIGHLIGHT

    figure = go.Figure()
    if nav_df.empty:
        return figure

    ordered = nav_df.sort_values("nav_date")
    # 分红基金必须用复权净值（与业绩走势、全收益基准 000300S 口径一致）；无复权时回退单位净值
    col = "adjusted_nav" if "adjusted_nav" in ordered.columns and ordered["adjusted_nav"].notna().any() else "unit_nav"
    base = float(ordered[col].iloc[0])
    cum_return = (ordered[col] / base - 1.0) * 100.0

    figure.add_trace(
        go.Scatter(
            x=ordered["nav_date"],
            y=cum_return,
            mode="lines",
            name="本基金",
            line=dict(color=COLOR_FUND_HIGHLIGHT, width=2.5),
            hovertemplate="%{x|%Y-%m-%d}<br>累计收益：%{y:.2f}%<extra></extra>",
        )
    )

    if not benchmark_df.empty:
        bench = benchmark_df.sort_values("nav_date")
        bench_base = float(bench["benchmark"].iloc[0])
        bench_return = (bench["benchmark"] / bench_base - 1.0) * 100.0
        figure.add_trace(
            go.Scatter(
                x=bench["nav_date"],
                y=bench_return,
                mode="lines",
                name=benchmark_name,
                # 对比线：极淡灰、细、实线（背景化，不抢主线）
                line=dict(color=COLOR_BENCHMARK, width=1, dash=None),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{benchmark_name}：%{{y:.2f}}%<extra></extra>",
            )
        )

    figure.update_layout(
        template="plotly_white",
        height=220,
        margin=dict(l=4, r=4, t=16, b=10),
        hovermode="x unified",
        dragmode=False,
        showlegend=show_legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    # 左侧只显示数字刻度（密度适中 nticks=7）、0 轴基准线加深、横向网格极淡、隐藏纵向网格、左右边距最小化
    figure.update_yaxes(
        zeroline=True, zerolinecolor="#B0B0B0", zerolinewidth=1.2,
        gridcolor="#F0F0F0", nticks=7,
    )
    figure.update_xaxes(showgrid=False, tickformat="%Y-%m-%d")
    return figure


def build_drawdown_area_chart(nav_df: pd.DataFrame) -> go.Figure:
    """最大回撤面积图（恒为负值，绿色填充）。"""
    from src.ui.theme import COLOR_DOWN

    figure = go.Figure()
    if nav_df.empty:
        return figure

    ordered = nav_df.sort_values("nav_date")
    drawdown_df = build_drawdown_series(ordered)

    figure.add_trace(
        go.Scatter(
            x=drawdown_df["nav_date"],
            y=drawdown_df["drawdown_pct"],
            mode="lines",
            name="回撤 (%)",
            line=dict(color=COLOR_DOWN, width=2),
            fill="tozeroy",
            fillcolor=_hex_with_alpha(COLOR_DOWN, 0.13),
            hovertemplate="%{x|%Y-%m-%d}<br>回撤：%{y:.2f}%<extra></extra>",
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=180,
        margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
        dragmode=False,
        yaxis_title="回撤 (%)",
    )
    figure.update_yaxes(zeroline=True, zerolinecolor="#D9D9D9", gridcolor="#F0F1F3")
    figure.update_xaxes(showgrid=True, gridcolor="#F0F1F3", tickformat="%Y-%m-%d")
    return figure


def build_sparkline(nav_df: pd.DataFrame, height: int = 60) -> go.Figure:
    """卡片右侧迷你走势图（无坐标轴、无网格）。"""
    figure = go.Figure()
    if nav_df.empty:
        return figure

    ordered = nav_df.sort_values("nav_date")
    direction = _series_direction(ordered)
    color = "#E64A3D" if direction >= 0 else "#00B578"

    figure.add_trace(
        go.Scatter(
            x=ordered["nav_date"],
            y=ordered["unit_nav"],
            mode="lines",
            line=dict(color=color, width=1.8),
            fill="tozeroy",
            fillcolor=_hex_with_alpha(color, 0.10),
            hovertemplate="%{x|%Y-%m-%d}<br>净值：%{y:.4f}<extra></extra>",
        )
    )
    figure.update_layout(
        height=height,
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        hovermode="x unified",
    )
    return figure


def build_strategy_scores_chart(factors_df: pd.DataFrame) -> go.Figure:
    """策略 A/B 得分走势折线图（含触发线 80）。"""
    from src.ui.theme import CHART_COLORS

    figure = go.Figure()
    if factors_df.empty:
        return figure

    ordered = factors_df.sort_values("trade_date")
    figure.add_trace(
        go.Scatter(
            x=ordered["trade_date"],
            y=ordered["score_a"],
            name="A 得分",
            line=dict(color=CHART_COLORS[0], width=2),
            hovertemplate="%{x|%Y-%m-%d}<br>A 得分：%{y:.1f}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=ordered["trade_date"],
            y=ordered["score_b"],
            name="B 得分",
            line=dict(color=CHART_COLORS[1], width=2),
            hovertemplate="%{x|%Y-%m-%d}<br>B 得分：%{y:.1f}<extra></extra>",
        )
    )
    figure.add_hline(
        y=80,
        line_dash="dash",
        line_color="#8A8F99",
        annotation_text="触发线 80",
        annotation_position="top right",
    )
    figure.update_layout(
        template="plotly_white",
        height=220,
        margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
        dragmode=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    figure.update_yaxes(title_text="得分", gridcolor="#F0F1F3")
    figure.update_xaxes(showgrid=True, gridcolor="#F0F1F3", tickformat="%Y-%m-%d")
    return figure


def build_dividend_history_chart(dividend_df: pd.DataFrame) -> go.Figure:
    """分红历史折线图：累计每份分红（元）随除息日变化。"""
    from src.ui.theme import COLOR_PRIMARY

    figure = go.Figure()
    if dividend_df.empty:
        return figure

    ordered = dividend_df.sort_values("ex_date")
    cum_dividend = ordered["dividend_per_unit"].cumsum()
    figure.add_trace(
        go.Scatter(
            x=ordered["ex_date"],
            y=cum_dividend,
            mode="lines+markers",
            name="累计每份分红 (元)",
            line=dict(color=COLOR_PRIMARY, width=2),
            marker=dict(size=6),
            hovertemplate="%{x|%Y-%m-%d}<br>累计每份分红：%{y:.4f} 元<extra></extra>",
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=190,
        margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
        dragmode=False,
    )
    figure.update_yaxes(title_text="累计每份分红 (元)", gridcolor="#F0F1F3")
    figure.update_xaxes(showgrid=True, gridcolor="#F0F1F3", tickformat="%Y-%m-%d")
    return figure


def build_performance_chart(nav_df: pd.DataFrame, show_legend: bool = True) -> go.Figure:
    """业绩走势折线图：复权净值累计收益率（%）时间序列，区间首日归一化为 0%。

    分红基金（008163 每月分红）必须用复权净值，单位净值会严重低估收益；
    x 轴为日期（YYYY-MM-DD），主基金线固定高亮橙（无论是否对比都一致）。
    """
    from src.ui.theme import COLOR_FUND_HIGHLIGHT

    figure = go.Figure()
    if nav_df.empty or "adjusted_nav" not in nav_df.columns:
        return figure

    ordered = nav_df.sort_values("nav_date")
    base = float(ordered["adjusted_nav"].iloc[0])
    if not base:
        return figure
    cum_return = (ordered["adjusted_nav"] / base - 1.0) * 100.0

    figure.add_trace(
        go.Scatter(
            x=ordered["nav_date"],
            y=cum_return,
            mode="lines",
            name="业绩走势",
            line=dict(color=COLOR_FUND_HIGHLIGHT, width=2.5),
            fill="tozeroy",
            fillcolor=_hex_with_alpha(COLOR_FUND_HIGHLIGHT, 0.04),
            hovertemplate="%{x|%Y-%m-%d}<br>累计收益：%{y:.2f}%<extra></extra>",
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=220,
        margin=dict(l=4, r=4, t=16, b=10),
        hovermode="x unified",
        dragmode=False,
        showlegend=show_legend,
    )
    # 左侧只显示数字刻度（密度适中 nticks=7）、0 轴基准线加深、横向网格极淡、隐藏纵向网格、左右边距最小化
    figure.update_yaxes(
        zeroline=True, zerolinecolor="#B0B0B0", zerolinewidth=1.2,
        gridcolor="#F0F0F0", nticks=7,
    )
    figure.update_xaxes(showgrid=False, tickformat="%Y-%m-%d")
    return figure
