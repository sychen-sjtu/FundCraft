"""基金详情页：大净值头部 + 折线图行情 + 策略指标（默认不计算，按钮触发）+ 分红。

设计原则：
- 全部用折线图展示，不使用表格。
- 「策略指标」（即策略因子）默认不计算，点击「计算策略指标」后展示当日指标 + 变化趋势折线图。
- 净值走势 / 最大回撤 / 分红记录 默认折叠。
- 「红利低波」类基金展示该基金自己的策略指标。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.ui import store
from src.ui.charts import (
    build_cumulative_vs_benchmark_chart,
    build_dividend_history_chart,
    build_drawdown_area_chart,
    build_nav_area_chart,
    build_performance_chart,
    build_strategy_scores_chart,
)
from src.ui.theme import PLOTLY_CONFIG, detail_head_html


def _render_back_and_header(code: str) -> None:
    meta = store.get_fund_meta(code)
    latest = store.get_latest_nav(code)

    col_back, col_head = st.columns([1, 5])
    with col_back:
        if st.button("← 返回", use_container_width=True):
            st.session_state["page"] = "overview"
            st.session_state.pop("selected_fund", None)
            st.rerun()
    with col_head:
        nav_date = pd.Timestamp(latest["nav_date"]).strftime("%Y-%m-%d")
        st.markdown(
            detail_head_html(
                fund_name=meta["fund_name"],
                fund_code=code,
                category=meta["category"],
                latest_nav=latest["unit_nav"],
                daily_change=latest["daily_return_pct"],
                nav_date=f"净值日期：{nav_date}",
            ),
            unsafe_allow_html=True,
        )


def _render_evaluation(code: str) -> None:
    """🔍 基金评估：渐进式三层就绪度（数据齐到哪层出到哪层，绝不模拟/估算）。"""
    st.divider()
    st.subheader("🔍 基金评估")
    ev = store.get_fund_evaluation(code)

    status = ev.get("status", "blocked")
    status_badge = {"ok": "🟢 完整", "partial": "🟡 部分", "blocked": "🔴 暂无"}.get(status, status)
    c1, c2, c3 = st.columns([1, 2, 4])
    with c1:
        st.markdown(f"**状态**：{status_badge}")
    with c2:
        missing = ev.get("missing", [])
        st.markdown(f"**缺失**：{'、'.join(missing) if missing else '无'}")
    with c3:
        st.markdown(f"**提示**：{ev.get('hint', '')}")

    layers = ev.get("layers", {})
    cols = st.columns(3)
    for col, (key, label) in zip(cols, [("fund", "基金层"), ("index", "指数层"), ("strategy", "策略层")]):
        with col:
            layer = layers.get(key)
            if layer and layer.get("available"):
                st.success(f"**{label}**：✅ 可用")
                detail_text = _layer_detail_text(key, layer)
                if detail_text:
                    st.caption(detail_text)
            else:
                st.info(f"**{label}**：⬜ 暂无数据")


def _layer_detail_text(key: str, layer: dict) -> str:
    """分层就绪度的可读摘要（全部来自真实数据量，不模拟）。"""
    if key == "fund":
        return (
            f"净值 {layer.get('nav_rows', 0)} 条 · 分红 {layer.get('dividend_rows', 0)} 条"
            f" · {layer.get('start_date', '?')} ~ {layer.get('end_date', '?')}"
        )
    if key == "index":
        return (
            f"指数行情 {layer.get('price_rows', 0)} 条"
            f" · {layer.get('start_date', '?')} ~ {layer.get('end_date', '?')}"
        )
    if key == "strategy":
        return (
            f"指数 {layer.get('price_rows', 0)} 条 · 股息率 {layer.get('dy_rows', 0)} 条"
            f" · PE {layer.get('pe_rows', 0)} 条 · 利率 {layer.get('rate_rows', 0)} 条"
        )
    return ""


def _render_performance_chart(nav_df: pd.DataFrame) -> None:
    """业绩走势折线图：复权净值累计收益率（%）时间序列，随上方「时间范围」胶囊联动。

    分红基金（008163 每月分红）必须用复权净值，单位净值会严重低估收益；
    区间首日归一化为 0%，与累计收益率口径类似，但按复权净值计算。
    """
    st.caption("业绩走势（复权净值累计收益率 %，随上方时间范围联动）")
    st.plotly_chart(
        build_performance_chart(nav_df),
        width="stretch",
        config=PLOTLY_CONFIG,
    )


def _render_strategy_signal(code: str) -> None:
    """策略指标：默认不计算，点按钮后计算并突出显示当日策略指标 + 变化趋势折线图。"""
    st.divider()
    st.subheader("🎯 策略指标")

    computed_key = f"strategy_computed_{code}"
    if not st.session_state.get(computed_key, False):
        st.caption("策略指标（策略因子）默认不计算，计算较耗时。点击下方按钮开始计算，计算后展示当日策略指标与变化趋势。")
        if st.button("🧮 计算策略指标", type="primary"):
            st.session_state[computed_key] = True
            st.rerun()
        return

    overview = store.get_strategy_overview(code)
    if not overview:
        st.info("暂无策略因子数据，请先在数据管理页执行刷新。")
        return

    st.markdown(
        f'<span class="fc-today-tag">今日</span><b>当日策略指标</b>'
        f'<span style="font-size:13px;color:#8A8F99;margin-left:8px;">信号日期：{pd.Timestamp(overview["trade_date"]).strftime("%Y-%m-%d")}</span>',
        unsafe_allow_html=True,
    )

    signal_a = overview["signal_a"]
    signal_b = overview["signal_b"]

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        badge_a = '<span class="fc-signal-buy">买入</span>' if signal_a else '<span class="fc-signal-wait">观望</span>'
        st.markdown(
            f'<div class="fc-metric-today"><div class="fc-metric-label">A 策略得分</div>'
            f'<div class="fc-metric-value">{overview["score_a"]:.1f}</div>'
            f'<div style="margin-top:6px;">{badge_a}</div></div>',
            unsafe_allow_html=True,
        )
    with c2:
        badge_b = '<span class="fc-signal-buy">买入</span>' if signal_b else '<span class="fc-signal-wait">观望</span>'
        st.markdown(
            f'<div class="fc-metric-today"><div class="fc-metric-label">B 策略得分</div>'
            f'<div class="fc-metric-value">{overview["score_b"]:.1f}</div>'
            f'<div style="margin-top:6px;">{badge_b}</div></div>',
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f'<div class="fc-metric-today"><div class="fc-metric-label">指数股息率</div>'
            f'<div class="fc-metric-value">{overview["dividend_yield"]:.2f}%</div></div>',
            unsafe_allow_html=True,
        )
    with c4:
        st.markdown(
            f'<div class="fc-metric-today"><div class="fc-metric-label">股息率-10Y 利差</div>'
            f'<div class="fc-metric-value">{overview["spread"]:.2f}%</div></div>',
            unsafe_allow_html=True,
        )

    # 策略指标变化趋势折线图（A/B 得分）
    factors = store.get_strategy_factors(code, tail=180)
    if not factors.empty:
        st.markdown("**策略指标变化趋势（A/B 得分）**")
        st.plotly_chart(build_strategy_scores_chart(factors), width="stretch", config=PLOTLY_CONFIG)

    # ---------- 策略回测占位（字段保留，功能未接入：真实回测引擎见 strategy_backtest.py） ----------
    st.divider()
    st.subheader("🧪 策略回测")
    backtest_key = f"strategy_backtest_{code}"
    if not st.session_state.get(backtest_key, False):
        st.caption("策略回测功能暂未接入，字段先保留。")
        if st.button("🧪 计算策略回测"):
            st.session_state[backtest_key] = True
            st.rerun()
        return

    bt = store.get_backtest_overview()
    if bt:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("XIRR 年化", f'{bt["xirr_pct"]:.1f}%')
        c2.metric("组合最大回撤", f'{bt["max_drawdown_pct"]:.1f}%')
        c3.metric("信号触发", f'{bt["buy_count"]} 次')
        c4.metric("回测区间", str(bt["period"]))
    else:
        st.info("暂无回测数据（回测功能待接入，不提供模拟数值）。")


def _render_dividends(code: str) -> None:
    """分红记录：折线图展示累计每份分红。"""
    dividend_df = store.get_dividends(code)
    if dividend_df.empty:
        st.caption("暂无分红记录。")
        return
    st.plotly_chart(build_dividend_history_chart(dividend_df), width="stretch", config=PLOTLY_CONFIG)


def render() -> None:
    if not store.is_connected():
        st.warning("请先在侧边栏「数据连接」输入解密口令并连接 Supabase。")
        return

    code = str(st.session_state.get("selected_fund", ""))
    if not code or code not in store.get_fund_codes():
        st.warning("未选择基金，请返回总览页。")
        return

    meta = store.get_fund_meta(code)

    _render_back_and_header(code)
    st.markdown("<br>", unsafe_allow_html=True)

    # ---------- 基金评估（渐进式三层就绪度） ----------
    _render_evaluation(code)

    # ---------- 时间范围胶囊（联动下方所有图表与指标） ----------
    range_key = st.segmented_control(
        "时间范围",
        options=store.RANGE_OPTIONS,
        default="近1年",
        selection_mode="single",
        label_visibility="collapsed",
    )

    nav_df = store.get_nav_history(code, range_key=range_key)
    benchmark_df = store.get_benchmark(range_key=range_key)

    # ---------- 业绩走势折线图（复权净值累计收益率，随时间范围联动） ----------
    _render_performance_chart(nav_df)

    # ---------- 累计收益率 vs 沪深300（主图） ----------
    st.divider()
    st.subheader("📈 累计收益率 vs 沪深300")
    st.plotly_chart(
        build_cumulative_vs_benchmark_chart(nav_df, benchmark_df),
        width="stretch",
        config=PLOTLY_CONFIG,
    )

    # ---------- 净值走势（默认折叠） ----------
    with st.expander("📉 净值走势", expanded=False):
        st.plotly_chart(build_nav_area_chart(nav_df), width="stretch", config=PLOTLY_CONFIG)

    # ---------- 最大回撤（默认折叠） ----------
    with st.expander("📉 最大回撤", expanded=False):
        st.plotly_chart(build_drawdown_area_chart(nav_df), width="stretch", config=PLOTLY_CONFIG)

    # ---------- 策略信号（红利低波） ----------
    if meta["panel"] == "红利低波":
        _render_strategy_signal(code)

    # ---------- 分红记录（默认折叠，折线图） ----------
    with st.expander("💰 分红记录", expanded=False):
        _render_dividends(code)
