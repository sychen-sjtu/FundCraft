"""📊 总览页：市场指数条 + 自选基金（按类别分组）卡片列表。

对标支付宝理财首页：
- 顶部市场指数条展示可配置的大盘指数简略信息（TOML [ui.market_indexes].codes）。
- 自选基金按类别分组展示；每只基金一张精简卡片（名称/代码/类别/区间收益，
  不显示净值与走势图），底部提供「查看详情」入口。
- 概览指标（自选基金/净值更新/策略基金/最近同步）只保留一行简略说明，不占大版面。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.config import load_fund_categories
from src.ui import store
from src.ui.theme import fund_card_html, render_index_bar


def _render_bond_comparison(codes: list[str]) -> None:
    """固收+ 基金核心指标对比表（历史年化/近1月/近3月/最大回撤/卡玛/年限/规模）。"""
    data = store.get_funds_bond_comparison(codes)
    if data.empty:
        st.caption("暂无对比数据。")
        return

    def _pct_cell(value) -> str:
        if value is None:
            return '<span class="fc-flat fc-num">—</span>'
        value = float(value)
        cls = "fc-up" if value > 0 else ("fc-down" if value < 0 else "fc-flat")
        sign = "+" if value > 0 else ""
        return f'<span class="{cls} fc-num">{sign}{value:.2f}%</span>'

    def _num_cell(value, unit: str = "") -> str:
        if value is None:
            return '<span class="fc-flat fc-num">—</span>'
        return f'<span class="fc-num">{float(value):.2f}{unit}</span>'

    rows_html = []
    for row in data.itertuples(index=False):
        name = str(row.fund_name) if row.fund_name else str(row.fund_code)
        ann_note = f'<div style="font-size:11px;color:#B0B6BF;">自 {int(row.inception_year)} 年</div>' if row.inception_year else ""
        rows_html.append(
            "<tr>"
            f"<td><b>{name}</b><div style='font-size:11px;color:#B0B6BF;'>{row.fund_code}</div></td>"
            f"<td class='num'>{_pct_cell(row.annualized_return)}{ann_note}</td>"
            f"<td class='num'>{_pct_cell(row.return_1m)}</td>"
            f"<td class='num'>{_pct_cell(row.return_3m)}</td>"
            f"<td class='num'>{_pct_cell(row.max_drawdown)}</td>"
            f"<td class='num'>{_num_cell(row.calmar_ratio)}</td>"
            f"<td class='num'>{_num_cell(row.fund_age_years, ' 年')}</td>"
            f"<td class='num'>{_num_cell(row.fund_scale, ' 亿')}</td>"
            "</tr>"
        )
    st.markdown(
        '<div class="fc-nav-table-wrap"><table class="fc-nav-table">'
        "<thead><tr><th>基金</th><th class='num'>历史年化收益</th><th class='num'>近1月</th>"
        "<th class='num'>近3月</th><th class='num'>最大回撤</th><th class='num'>卡玛比率</th>"
        "<th class='num'>基金年限</th><th class='num'>基金规模</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></div>",
        unsafe_allow_html=True,
    )


def _render_bond_risk_comparison(codes: list[str]) -> None:
    """债基 风控与持仓对比表：近1年最大回撤 / 最长回撤修复天数 / 底层安全性（类别占比）。

    回撤来自落库复权净值真实数据；底层安全性来自 akshare 最新报告期债券持仓（按类别归类）。
    """
    data = store.get_bond_risk_comparison(codes)
    if data.empty:
        st.caption("暂无对比数据。")
        return

    def _dd_cell(value) -> str:
        if value is None or pd.isna(value):
            return '<span class="fc-flat fc-num">—</span>'
        value = float(value)
        cls = "fc-down" if value < 0 else "fc-flat"
        return f'<span class="{cls} fc-num">{value:.2f}%</span>'

    def _days_cell(days, range_txt) -> str:
        if days is None or pd.isna(days):
            return '<span class="fc-flat fc-num">—</span>'
        note = f"<div style='font-size:11px;color:#B0B6BF;'>{range_txt}</div>" if range_txt else ""
        return f'<span class="fc-num">{int(days)} 交易日</span>{note}'

    rows_html = []
    for row in data.itertuples(index=False):
        name = str(row.fund_name) if row.fund_name else str(row.fund_code)
        dd1y_note = (
            f"<div style='font-size:11px;color:#B0B6BF;'>全历史 {row.max_drawdown_all:.2f}%</div>"
            if row.max_drawdown_all is not None and not pd.isna(row.max_drawdown_all)
            else ""
        )
        recover_note = ""
        if row.recover_all_days is not None and not pd.isna(row.recover_all_days):
            recover_note = f"<div style='font-size:11px;color:#B0B6BF;'>近1年最长 · 全历史最长 {int(row.recover_all_days)}</div>"
        elif row.recover_1y_days is not None and not pd.isna(row.recover_1y_days):
            recover_note = f"<div style='font-size:11px;color:#B0B6BF;'>近1年最长</div>"
        holdings_note = f"<div style='font-size:11px;color:#B0B6BF;'>{row.holdings_note}</div>"
        rows_html.append(
            "<tr>"
            f"<td><b>{name}</b><div style='font-size:11px;color:#B0B6BF;'>{row.fund_code}</div></td>"
            f"<td class='num'>{_dd_cell(row.max_drawdown_1y)}{dd1y_note}</td>"
            f"<td class='num'>{_days_cell(row.recover_1y_days, row.recover_1y_range)}{recover_note}</td>"
            f"<td><span class='fc-num'>{row.holdings_summary}</span>{holdings_note}</td>"
            "</tr>"
        )
    st.markdown(
        '<div class="fc-nav-table-wrap"><table class="fc-nav-table">'
        "<thead><tr><th>基金</th><th class='num'>近1年最大回撤</th><th class='num'>最长回撤修复天数</th>"
        "<th>底层安全性（类别占比）</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "回撤/修复天数来自落库复权净值（真实数据）· 底层安全性=akshare 最新报告期披露债券持仓按名称归类（国开/国债/政金/信用/可转债）"
    )


def render() -> None:
    st.title("📊 我的基金")
    st.caption("自选基金 · 仅用于可视化与决策参考")

    if not store.is_connected():
        st.info("请先在侧边栏「数据连接」输入解密口令并连接 Supabase。")
        return

    # ---------- 顶部市场指数条（可配置） ----------
    render_index_bar(store.get_market_indexes())
    st.markdown("<br>", unsafe_allow_html=True)

    # ---------- 概览简略说明（一行，不占大版面） ----------
    metrics = store.get_overview_metrics()
    st.caption(
        f"自选基金 {metrics['fund_count']} 只 · "
        f"净值更新至 {metrics['latest_nav_date']} · "
        f"策略基金 {metrics['strategy_fund_count']} 只 · "
        f"最近同步 {metrics['last_sync']}"
    )

    st.markdown("<br>", unsafe_allow_html=True)

    # ---------- 自选基金：按类别分组 ----------
    overview = store.get_all_funds_overview()
    if overview.empty:
        st.info("暂无自选基金。")
        return

    # 类别顺序沿用 TOML 配置顺序；配置外的类别归入「其他」
    ordered = [c.name for c in load_fund_categories(store.PROJECT_ROOT).values()]
    extra = sorted(c for c in overview["category"].dropna().unique() if c not in ordered)
    for category in ordered + extra:
        group = overview[overview["category"] == category]
        if group.empty:
            continue

        st.subheader(f"⭐ {category}")

        for row in group.itertuples(index=False):
            code = row.fund_code
            period = {"近1周": row.return_1w, "近1月": row.return_1m, "近3月": row.return_3m}
            card_html = fund_card_html(
                fund_name=row.fund_name,
                fund_code=code,
                category=row.category,
                latest_nav=None,
                daily_change=None,
                period_returns=period,
                show_nav=False,
            )
            with st.container(border=True):
                left, right = st.columns([4, 1], vertical_alignment="center")
                with left:
                    st.markdown(card_html, unsafe_allow_html=True)
                with right:
                    if st.button("查看详情 →", key=f"go_{code}", use_container_width=True):
                        st.session_state["page"] = "detail"
                        st.session_state["selected_fund"] = code
                        st.rerun()

        # 固收+ 核心指标对比表（默认加载）
        if category == "固收+":
            _render_bond_comparison([str(r.fund_code) for r in group.itertuples(index=False)])
        # 债基 风控与持仓对比表（最大回撤/回撤修复天数/底层安全性类别占比）
        if category == "债基":
            _render_bond_risk_comparison([str(r.fund_code) for r in group.itertuples(index=False)])
