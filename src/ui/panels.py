"""类别面板：根据基金类别（config.panel）渲染不同的「基金看板」。

设计：面板注册表 PANEL_REGISTRY 把「面板类型名」映射到渲染函数。
- 配置（secrets.toml）里每个类别带 panel 字段，如 "红利低波" / "固收" / "净值"。
- 每个基金一块看板：基础信息（名称/代码/类别/类型/跟踪指数）+ 近一周/一月收益；
  类别面板在此基础上追加各自的信息块。红利低波把「策略信号」放在最前面。
- 净值/回撤图与明细表等详细信息收进折叠区，避免默认堆砌一年数据。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.storage.supabase_store import fetch_daily_factors, fetch_fund_dividends
from src.ui.charts import build_dividend_yield_chart, build_nav_and_drawdown_chart


def _compute_period_return(nav_df: pd.DataFrame, days: int = 7) -> float | None:
    """近 days 个自然日的收益率（%），数据不足时返回 None。"""
    ordered = nav_df.sort_values("nav_date")
    if len(ordered) < 2:
        return None
    latest = float(ordered["unit_nav"].iloc[-1])
    cutoff = pd.Timestamp(ordered["nav_date"].iloc[-1]) - pd.Timedelta(days=days)
    past = ordered[ordered["nav_date"] <= cutoff]
    base = float(past["unit_nav"].iloc[-1]) if not past.empty else float(ordered["unit_nav"].iloc[0])
    return (latest / base - 1.0) * 100.0 if base else None


def _render_basic_board(
    client,
    fund_code: str,
    fund_name: str,
    category_name: str,
    nav_df: pd.DataFrame,
    summary_row,
    profile_row,
) -> None:
    """基础看板：基金名/代码/类别/类型/跟踪指数 + 近一周/一月收益 + 最新净值。"""
    title = f"{fund_name}（{fund_code}）" if fund_name else f"基金 {fund_code}"
    st.markdown(f"### {title}")

    info_bits = [f"类别：{category_name}"]
    if profile_row is not None:
        if profile_row.get("fund_type"):
            info_bits.append(f"类型：{profile_row['fund_type']}")
        if profile_row.get("tracking_index"):
            info_bits.append(f"跟踪：{profile_row['tracking_index']}")
    if info_bits:
        st.caption(" ｜ ".join(info_bits))

    week_return = _compute_period_return(nav_df, days=7)
    month_return = _compute_period_return(nav_df, days=30)
    latest_nav = float(nav_df["unit_nav"].iloc[-1]) if not nav_df.empty else None

    c1, c2, c3 = st.columns(3)
    c1.metric("近一周收益", "—" if week_return is None else f"{week_return:+.2f}%")
    c2.metric("近一月收益", "—" if month_return is None else f"{month_return:+.2f}%")
    c3.metric("最新净值", "—" if latest_nav is None else f"{latest_nav:.4f}")


def _render_nav_section(client, fund_code: str, nav_df: pd.DataFrame) -> None:
    """净值/回撤走势 + 明细表 + 下载（收进折叠区，避免默认展示一年数据）。"""
    with st.expander("净值与回撤走势"):
        st.plotly_chart(build_nav_and_drawdown_chart(nav_df.assign(fund_code=fund_code)), width="stretch")
        preview_df = nav_df.copy()
        preview_df["nav_date"] = preview_df["nav_date"].dt.date.astype(str)
        st.dataframe(preview_df, width="stretch", height=280, hide_index=True)
        st.download_button(
            label="下载该基金明细 CSV",
            data=preview_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{fund_code}_nav.csv",
            mime="text/csv",
        )


def _render_strategy_signals(client, fund_code: str) -> None:
    """策略信号（红利低波面板置顶展示）。"""
    try:
        factors_df = fetch_daily_factors(client, fund_code)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"策略因子读取失败：{exc}")
        factors_df = pd.DataFrame()

    if factors_df.empty:
        st.info("暂无策略因子数据，请先在侧边栏「刷新数据」。")
        return

    latest = factors_df.sort_values("trade_date").iloc[-1]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("A 策略得分", f"{latest['score_a']:.1f}", "买入" if latest.get("signal_a") else "观望")
    col2.metric("B 策略得分", f"{latest['score_b']:.1f}", "买入" if latest.get("signal_b") else "观望")
    col3.metric("指数股息率", f"{latest['dividend_yield']:.2f}%")
    col4.metric("股息率-10Y 利差", f"{latest['spread']:.2f}%")

    with st.expander("最近 10 个交易日的得分明细"):
        view = factors_df.sort_values("trade_date").tail(10).copy()
        view["trade_date"] = view["trade_date"].dt.date.astype(str)
        view = view.rename(
            columns={
                "trade_date": "日期",
                "dividend_yield": "股息率(%)",
                "score_a": "A得分",
                "signal_a": "A信号",
                "score_b": "B得分",
                "signal_b": "B信号",
            }
        )
        st.dataframe(
            view[["日期", "股息率(%)", "A得分", "A信号", "B得分", "B信号"]],
            width="stretch",
            hide_index=True,
        )


def _render_dividends(client, fund_code: str, nav_df: pd.DataFrame) -> None:
    """分红与合成股息率。"""
    try:
        dividend_df = fetch_fund_dividends(client, fund_code)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"分红数据读取失败：{exc}")
        dividend_df = pd.DataFrame(columns=["fund_code", "ex_date", "dividend_per_unit"])

    if dividend_df.empty:
        st.info("暂无分红记录。")
        return

    st.plotly_chart(build_dividend_yield_chart(nav_df, dividend_df), width="stretch")
    div_view = dividend_df.copy()
    div_view["ex_date"] = div_view["ex_date"].dt.date.astype(str)
    div_view = div_view.rename(columns={"ex_date": "除息日", "dividend_per_unit": "每份分红(元)"})
    st.dataframe(div_view[["除息日", "每份分红(元)"]], width="stretch", hide_index=True)


def render_basic_panel(client, fund_code: str, fund_name: str, category_name: str, nav_df: pd.DataFrame, summary_row, profile_row) -> None:
    """净值/固收面板：基础看板 + 折叠的净值明细。"""
    _render_basic_board(client, fund_code, fund_name, category_name, nav_df, summary_row, profile_row)
    st.divider()
    _render_nav_section(client, fund_code, nav_df)


def render_dividend_lowvol_panel(client, fund_code: str, fund_name: str, category_name: str, nav_df: pd.DataFrame, summary_row, profile_row) -> None:
    """红利低波面板：基础看板 + 策略信号（置顶）+ 分红/股息率 + 净值明细。"""
    _render_basic_board(client, fund_code, fund_name, category_name, nav_df, summary_row, profile_row)

    st.divider()
    st.subheader("🎯 策略信号")
    _render_strategy_signals(client, fund_code)

    st.divider()
    st.subheader("基金分红（参考）")
    st.caption("以下为基金自身的分红记录（非策略因子）；策略股息率采用对应底层指数股息率。")
    with st.expander("查看分红记录"):
        _render_dividends(client, fund_code, nav_df)

    st.divider()
    _render_nav_section(client, fund_code, nav_df)


# 面板注册表：新增类别面板时在此登记
PANEL_REGISTRY = {
    "净值": render_basic_panel,
    "固收": render_basic_panel,
    "红利低波": render_dividend_lowvol_panel,
}


def render_panel(
    panel_type: str,
    client,
    fund_code: str,
    fund_name: str,
    category_name: str,
    nav_df: pd.DataFrame,
    summary_row,
    profile_row,
) -> None:
    """按面板类型渲染基金看板；未知类型回退到「净值」面板。"""
    renderer = PANEL_REGISTRY.get(panel_type, render_basic_panel)
    renderer(client, fund_code, fund_name, category_name, nav_df, summary_row, profile_row)
