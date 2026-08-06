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

    # ---------- 策略回测概览（默认不计算，按钮触发；先算策略指标后才能算回测） ----------
    st.divider()
    st.subheader("🧪 策略回测")
    backtest_key = f"strategy_backtest_{code}"
    if not st.session_state.get(backtest_key, False):
        st.caption("策略回测默认不计算，计算较耗时。点击下方按钮开始计算，计算后展示回测结果。")
        if st.button("🧪 计算策略回测", type="primary"):
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
        st.caption("说明：回测为模拟数值，仅用于展示 UI；真实回测请参考 `src/indicators/strategy_backtest.py`。")


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
