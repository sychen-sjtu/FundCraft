"""📊 总览页：市场指数条 + 概览指标 + 自选基金卡片列表。

对标支付宝理财首页：一只基金一张卡片，卡片展示最新净值、日涨跌幅、
各区间收益与迷你走势图，点击「查看详情」进入基金详情页。
"""

from __future__ import annotations

import streamlit as st

from src.ui import store
from src.ui.charts import build_sparkline
from src.ui.theme import PLOTLY_CONFIG, fund_card_html, render_index_bar, render_metric_card


def render() -> None:
    st.title("📊 我的基金")
    st.caption("自选基金 · 仅用于可视化与决策参考")

    if not store.is_connected():
        st.info("请先在侧边栏「数据连接」输入解密口令并连接 Supabase。")
        return

    # ---------- 顶部市场指数条 ----------
    render_index_bar(store.get_market_indexes())
    st.markdown("<br>", unsafe_allow_html=True)

    # ---------- 概览指标 ----------
    metrics = store.get_overview_metrics()
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        render_metric_card("自选基金", f'{metrics["fund_count"]} 只')
    with c2:
        render_metric_card("净值更新至", str(metrics["latest_nav_date"]))
    with c3:
        render_metric_card("策略基金", f'{metrics["strategy_fund_count"]} 只')
    with c4:
        render_metric_card("最近同步", str(metrics["last_sync"]))

    st.markdown("<br>", unsafe_allow_html=True)

    # ---------- 自选基金列表 ----------
    st.subheader("⭐ 我的自选")

    overview = store.get_all_funds_overview()
    if overview.empty:
        st.info("暂无自选基金。")
        return

    for row in overview.itertuples(index=False):
        code = row.fund_code
        period = {"近1周": row.return_1w, "近1月": row.return_1m, "近3月": row.return_3m}

        card_html = fund_card_html(
            fund_name=row.fund_name,
            fund_code=code,
            category=row.category,
            latest_nav=row.latest_nav,
            daily_change=row.daily_change_pct,
            period_returns=period,
        )

        with st.container(border=True):
            left, right = st.columns([3, 2], vertical_alignment="center")
            with left:
                st.markdown(card_html, unsafe_allow_html=True)
            with right:
                spark = build_sparkline(store.get_nav_history(code, range_key="近6月"), height=64)
                st.plotly_chart(spark, width="stretch", config=PLOTLY_CONFIG)

            # 底部操作行：类别 + 涨跌 + 进入详情
            op1, op2 = st.columns([4, 1])
            with op1:
                st.caption(f"{row.fund_type}")
            with op2:
                if st.button("查看详情 →", key=f"go_{code}", use_container_width=True):
                    st.session_state["page"] = "detail"
                    st.session_state["selected_fund"] = code
                    st.rerun()
