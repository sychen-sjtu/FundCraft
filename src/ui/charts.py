"""图表构建：FundCraft 各页面共用的 plotly 图表函数。"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

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


# ---------- RSI 动能看板（两层上下联动，共享 X 轴） ----------
# 主图/动能图信号标记配色（绿=买、紫=背离、橙=钝化警示，与用户方案一致）
_RSI_COLORS = {"A": "#00B578", "B": "#722ED1", "C": "#FA8C16"}
_RSI_SYMBOLS = {"A": "triangle-up", "B": "diamond", "C": "square"}
# RSI 超买超卖区间带与参考线
_RSI_LOW, _RSI_HIGH = 35.0, 65.0


def build_rsi_dashboard_chart(data: dict) -> go.Figure:
    """RSI 两层联动看板（共享 X 轴）。

    图1（主图）：复权净值（黑）+ 250 日均线（蓝虚线）+ 历史信号标注
        A 绿上箭头（强烈买入）/ B 紫菱形（底背离）/ C 橙方块（钝化警示）
    图2（动能图）：周 RSI12（粗实线）/ 周 RSI24（灰细线）/ 日 RSI6·RSI12（半透明辅助）
        + <35 浅绿（低吸区）/ >65 浅红（风险区）区间带 + 30/35/65/70 参考线 + 背离线（紫虚线）
    （T+60 前瞻收益图按用户要求暂不下沉展示；前瞻统计保留在当日指标卡与信号明细表）

    :param data: rsi.build_rsi_dashboard 的返回 dict（已按显示窗口裁剪）。
    """
    nav = data.get("nav", pd.DataFrame())
    figure = go.Figure()
    if nav.empty:
        return figure

    daily = data.get("daily", pd.DataFrame())
    weekly = data.get("weekly", pd.DataFrame())
    signals = data.get("signals", pd.DataFrame())
    divergences = data.get("divergences", [])

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.55, 0.45],
    )

    # ---------------- 图 1：复权净值 + 250MA + 信号标注 ----------------
    fig.add_trace(
        go.Scatter(
            x=nav["nav_date"], y=nav["adjusted_nav"], name="复权净值",
            line=dict(color="#1F2329", width=2.2),
            hovertemplate="%{x|%Y-%m-%d}<br>复权净值：%{y:.4f}<extra></extra>",
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=nav["nav_date"], y=nav["ma250"], name="250日均线",
            line=dict(color="#1677FF", width=1.4, dash="dash"),
            hovertemplate="%{x|%Y-%m-%d}<br>250MA：%{y:.4f}<extra></extra>",
        ),
        row=1, col=1,
    )

    # 信号标注（主图）
    if not signals.empty:
        for kind in ("A", "B", "C"):
            sel = signals[signals["kind"] == kind]
            if sel.empty:
                continue
            fig.add_trace(
                go.Scatter(
                    x=sel["nav_date"], y=sel["nav"], mode="markers",
                    name=kind, showlegend=False,
                    marker=dict(symbol=_RSI_SYMBOLS[kind], size=12, color=_RSI_COLORS[kind], line=dict(width=1, color="#FFFFFF")),
                    hovertemplate="%{x|%Y-%m-%d}<br>%{customdata}<br>复权净值：%{y:.4f}<extra></extra>",
                    customdata=sel["label"] if "label" in sel.columns else [kind] * len(sel),
                ),
                row=1, col=1,
            )

    # 底背离背离线（主图：价格低点连线）
    for dv in divergences:
        fig.add_trace(
            go.Scatter(
                x=[dv["price_x_prev"], dv["price_x_curr"]],
                y=[dv["price_y_prev"], dv["price_y_curr"]],
                mode="lines",
                line=dict(color=_RSI_COLORS["B"], width=1.2, dash="dot"),
                hoverinfo="skip",
                showlegend=False,
            ),
            row=1, col=1,
        )

    # ---------------- 图 2：周/日 RSI 双周期 + 超买超卖带 ----------------
    if not weekly.empty:
        fig.add_trace(
            go.Scatter(
                x=weekly["nav_date"], y=weekly["w_rsi12"], name="周RSI(12)",
                line=dict(color="#1F2329", width=2.2),
                hovertemplate="%{x|%Y-%m-%d}<br>周RSI(12)：%{y:.1f}<extra></extra>",
            ),
            row=2, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=weekly["nav_date"], y=weekly["w_rsi24"], name="周RSI(24)",
                line=dict(color="#8A8F99", width=1.4),
                hovertemplate="%{x|%Y-%m-%d}<br>周RSI(24)：%{y:.1f}<extra></extra>",
            ),
            row=2, col=1,
        )
    if not daily.empty:
        fig.add_trace(
            go.Scatter(
                x=daily["nav_date"], y=daily["rsi6"], name="日RSI(6)",
                line=dict(color="#1677FF", width=1.2), opacity=0.45,
                hovertemplate="%{x|%Y-%m-%d}<br>日RSI(6)：%{y:.1f}<extra></extra>",
            ),
            row=2, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=daily["nav_date"], y=daily["rsi12"], name="日RSI(12)",
                line=dict(color="#FA8C16", width=1.2), opacity=0.45,
                hovertemplate="%{x|%Y-%m-%d}<br>日RSI(12)：%{y:.1f}<extra></extra>",
            ),
            row=2, col=1,
        )

    # 底背离背离线（动能图：RSI 低点连线）
    for dv in divergences:
        fig.add_trace(
            go.Scatter(
                x=[dv["rsi_x_prev"], dv["rsi_x_curr"]],
                y=[dv["rsi_y_prev"], dv["rsi_y_curr"]],
                mode="lines",
                line=dict(color=_RSI_COLORS["B"], width=1.2, dash="dot"),
                hoverinfo="skip",
                showlegend=False,
            ),
            row=2, col=1,
        )

    # 区间带/参考线必须在本子图已有 trace 之后再添加（plotly 6.7 的 add_hrect/add_hline
    # 在目标子图尚无任何 trace 时会被静默丢弃，导致 35/65 区间带不渲染）
    # 区间带：<35 浅绿（低吸区） / >65 浅红（风险区）
    fig.add_hrect(y0=0, y1=_RSI_LOW, fillcolor="rgba(0,181,120,0.13)", line_width=0, row=2, col=1)
    fig.add_hrect(y0=_RSI_HIGH, y1=100, fillcolor="rgba(230,74,61,0.13)", line_width=0, row=2, col=1)
    # 参考线：30/70 点线（可见基准）+ 35/65 浅虚线（带边界）
    for y in (30, 70):
        fig.add_hline(y=y, line_dash="dot", line_color="#A6ADB8", line_width=1.2, row=2, col=1)
    for y in (_RSI_LOW, _RSI_HIGH):
        fig.add_hline(y=y, line_dash="dash", line_color="#C0C4CC", line_width=1, row=2, col=1)

    # ---------------- 布局 ----------------
    fig.update_layout(
        template="plotly_white",
        height=560,
        margin=dict(l=8, r=8, t=30, b=8),
        hovermode="x unified",
        dragmode=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0, font=dict(size=11)),
    )
    fig.update_yaxes(
        row=1, col=1,
        zeroline=True, zerolinecolor="#B0B0B0", zerolinewidth=1.2,
        gridcolor="#F0F0F0", nticks=7,
    )
    fig.update_yaxes(row=2, col=1, title_text="RSI", range=[0, 100], gridcolor="#F0F0F0", nticks=6)
    fig.update_xaxes(row=1, col=1, showgrid=False, tickformat="%Y-%m-%d")
    fig.update_xaxes(row=2, col=1, showgrid=True, gridcolor="#F0F0F0", tickformat="%Y-%m-%d")
    return fig


# ---------- 国债期货加仓信号（TF/T 双线 + 每日涨跌 + 历史买入点位） ----------
_BOND_COND1_COLOR = "#E64A3D"  # 条件1（优选/强化）红上三角 / 红竖线
_BOND_COND2_COLOR = "#722ED1"  # 条件2（连跌）紫菱形 / 紫竖线
_BOND_T_COLOR = "#9DB3CC"  # T(10年) 辅助线/柱（浅蓝灰，不抢 TF 主线）
_BOND_LEVEL_LABELS = {
    "cond1_preferred": "条件1·优选",
    "cond1_strengthen": "条件1·强化",
    "cond2_streak": "条件2·连跌",
}


def _add_bond_buy_markers(figure: go.Figure, data: dict, anchor: str = "tf") -> None:
    """在历史曲线上叠加买入点位标记（条件1 红上三角 / 条件2 紫菱形）。"""
    points = data.get("points", pd.DataFrame())
    if points.empty:
        return
    buys = points[points["trigger"]].copy()
    if buys.empty:
        return
    series = data.get(anchor, pd.DataFrame())
    if series.empty:
        return
    # 取锚定价格（TF 收盘）作为标记纵坐标
    merged = buys.merge(series[["trade_date", "rate_value"]].drop_duplicates("trade_date"), on="trade_date", how="left")

    cond1 = merged[merged["level"].isin(["cond1_preferred", "cond1_strengthen"])]
    cond2 = merged[merged["level"] == "cond2_streak"]
    for sel, color, symbol, label in (
        (cond1, _BOND_COND1_COLOR, "triangle-up", "条件1(优选/强化)"),
        (cond2, _BOND_COND2_COLOR, "diamond", "条件2(连跌)"),
    ):
        if sel.empty:
            continue
        figure.add_trace(
            go.Scatter(
                x=sel["trade_date"], y=sel["rate_value"], mode="markers",
                name=label,
                marker=dict(symbol=symbol, size=11, color=color, line=dict(width=1, color="#FFFFFF")),
                customdata=sel["level"].map(_BOND_LEVEL_LABELS),
                hovertemplate="%{x|%Y-%m-%d}<br>%{customdata}<br>TF：%{y:.3f}<extra></extra>",
            )
        )


def build_bond_futures_curve_chart(data: dict) -> go.Figure:
    """图1 历史曲线：TF + T 主力连续收盘双折线 + 历史买入点位标记。"""
    from src.ui.theme import COLOR_FUND_HIGHLIGHT

    figure = go.Figure()
    tf_df = data.get("tf", pd.DataFrame())
    if tf_df.empty:
        return figure

    tf = tf_df.sort_values("trade_date")
    figure.add_trace(
        go.Scatter(
            x=tf["trade_date"], y=tf["rate_value"], name="TF(5年)",
            line=dict(color=COLOR_FUND_HIGHLIGHT, width=2),
            hovertemplate="%{x|%Y-%m-%d}<br>TF：%{y:.3f}<extra></extra>",
        )
    )

    t_df = data.get("t", pd.DataFrame())
    if not t_df.empty:
        t = t_df.sort_values("trade_date")
        figure.add_trace(
            go.Scatter(
                x=t["trade_date"], y=t["rate_value"], name="T(10年)",
                line=dict(color=_BOND_T_COLOR, width=1.4),
                hovertemplate="%{x|%Y-%m-%d}<br>T：%{y:.3f}<extra></extra>",
            )
        )

    _add_bond_buy_markers(figure, data, anchor="tf")

    figure.update_layout(
        template="plotly_white",
        height=240,
        margin=dict(l=4, r=4, t=16, b=10),
        hovermode="x unified",
        dragmode=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=11)),
    )
    figure.update_yaxes(zeroline=True, zerolinecolor="#B0B0B0", zerolinewidth=1.2, gridcolor="#F0F0F0", nticks=7)
    figure.update_xaxes(showgrid=False, tickformat="%Y-%m-%d")
    return figure


def build_bond_futures_change_chart(data: dict) -> go.Figure:
    """图2 每日净涨跌：TF + T 日涨跌幅双柱（红涨绿跌）+ 买入点位竖线。"""
    from src.ui.theme import COLOR_DOWN, COLOR_UP

    figure = go.Figure()
    tf_df = data.get("tf", pd.DataFrame())
    if tf_df.empty:
        return figure

    tf = tf_df.sort_values("trade_date").copy()
    tf["pct"] = tf["rate_value"].astype(float).pct_change() * 100.0
    tf = tf.dropna(subset=["pct"])
    if tf.empty:
        return figure

    tf_colors = [COLOR_UP if value >= 0 else COLOR_DOWN for value in tf["pct"]]
    figure.add_trace(
        go.Bar(
            x=tf["trade_date"], y=tf["pct"], name="TF日涨跌",
            marker_color=tf_colors, opacity=0.7,
            hovertemplate="%{x|%Y-%m-%d}<br>TF：%{y:.2f}%<extra></extra>",
        )
    )

    t_df = data.get("t", pd.DataFrame())
    if not t_df.empty:
        t = t_df.sort_values("trade_date").copy()
        t["pct"] = t["rate_value"].astype(float).pct_change() * 100.0
        t = t.dropna(subset=["pct"])
        if not t.empty:
            t_colors = [COLOR_UP if value >= 0 else COLOR_DOWN for value in t["pct"]]
            figure.add_trace(
                go.Bar(
                    x=t["trade_date"], y=t["pct"], name="T日涨跌",
                    marker_color=t_colors, opacity=0.28,
                    hovertemplate="%{x|%Y-%m-%d}<br>T：%{y:.2f}%<extra></extra>",
                )
            )

    # 买入点位竖线（条件1 红实/虚、条件2 紫点）
    points = data.get("points", pd.DataFrame())
    if not points.empty:
        buys = points[points["trigger"]]
        for level, color, dash, label in (
            ("cond1_preferred", _BOND_COND1_COLOR, "solid", "条件1·优选"),
            ("cond1_strengthen", _BOND_COND1_COLOR, "dash", "条件1·强化"),
            ("cond2_streak", _BOND_COND2_COLOR, "dot", "条件2·连跌"),
        ):
            sel = buys[buys["level"] == level]
            if sel.empty:
                continue
            for d in sel["trade_date"]:
                figure.add_vline(x=d, line_width=1, line_dash=dash, line_color=color, opacity=0.55)
            # 图例（用空序列展示线型）
            figure.add_trace(
                go.Scatter(x=[None], y=[None], mode="lines", name=label, line=dict(color=color, dash=dash))
            )

    figure.update_layout(
        template="plotly_white",
        height=200,
        margin=dict(l=4, r=4, t=16, b=10),
        hovermode="x unified",
        dragmode=False,
        barmode="overlay",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=11)),
    )
    figure.update_yaxes(zeroline=True, zerolinecolor="#B0B0B0", zerolinewidth=1.2, gridcolor="#F0F0F0", nticks=7)
    figure.update_xaxes(showgrid=False, tickformat="%Y-%m-%d")
    return figure
