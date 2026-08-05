from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from src.config import load_access_passwords, load_fund_codes, load_supabase_settings, supabase_settings_ready
from src.indicators.fund_metrics import build_drawdown_series, compute_fund_metrics
from src.storage.strategy_sync_runner import refresh_with_client
from src.storage.supabase_store import (
    create_supabase_client,
    fetch_latest_sync_job,
    fetch_nav_history,
    list_fund_profiles,
    list_watermarks,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# UI 时间范围预设（默认近 1 年，避免每次全量拉取）
RANGE_OPTIONS = ["近1年", "近2年", "近3年", "近5年", "全部"]
DEFAULT_RANGE = "近1年"


def _format_ratio(value: float) -> str:
    return f"{value:.2f}%"


@st.cache_data(ttl=300)
def load_dashboard_data(
    secret_password: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, pd.DataFrame]:
    settings = load_supabase_settings(PROJECT_ROOT, secret_password=secret_password)
    client = create_supabase_client(settings)

    profiles_df = list_fund_profiles(client)
    latest_sync_df = fetch_latest_sync_job(client)
    watermarks_df = list_watermarks(client)

    nav_frames: list[pd.DataFrame] = []
    summary_rows = []

    for fund_code in profiles_df.get("fund_code", pd.Series(dtype=str)).tolist():
        nav_df = fetch_nav_history(client, fund_code, start_date=start_date, end_date=end_date)
        if nav_df.empty:
            continue
        nav_frames.append(nav_df)
        metrics = compute_fund_metrics(nav_df)
        summary_rows.append(
            {
                "fund_code": metrics.fund_code,
                "start_date": metrics.start_date,
                "end_date": metrics.end_date,
                "row_count": metrics.row_count,
                "start_unit_nav": metrics.start_unit_nav,
                "end_unit_nav": metrics.end_unit_nav,
                "cumulative_return_pct": metrics.cumulative_return_pct,
                "max_drawdown_pct": metrics.max_drawdown_pct,
                "annualized_volatility_pct": metrics.annualized_volatility_pct,
            }
        )

    summary_df = pd.DataFrame(summary_rows).sort_values("fund_code").reset_index(drop=True) if summary_rows else pd.DataFrame()
    combined_df = pd.concat(nav_frames, ignore_index=True) if nav_frames else pd.DataFrame(columns=["fund_code", "nav_date", "unit_nav", "daily_return"])
    if not combined_df.empty:
        combined_df["nav_date"] = pd.to_datetime(combined_df["nav_date"], errors="coerce")
        combined_df["unit_nav"] = pd.to_numeric(combined_df["unit_nav"], errors="coerce")
        combined_df = combined_df.dropna(subset=["nav_date", "unit_nav"])

    return {
        "profiles": profiles_df,
        "summary": summary_df,
        "combined": combined_df,
        "latest_sync": latest_sync_df,
        "watermarks": watermarks_df,
        "start_date": start_date,
        "end_date": end_date,
    }


def _build_overview_chart(summary_df: pd.DataFrame) -> go.Figure:
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


def _build_nav_and_drawdown_chart(nav_df: pd.DataFrame) -> go.Figure:
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

    colors = ["#2563EB", "#16A34A", "#F97316", "#8B5CF6", "#EF4444"]
    for index, fund_code in enumerate(sorted(nav_df["fund_code"].astype(str).unique().tolist())):
        fund_nav = nav_df[nav_df["fund_code"] == fund_code].sort_values("nav_date")
        drawdown_df = build_drawdown_series(fund_nav)
        color = colors[index % len(colors)]

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


def render_dashboard() -> None:
    st.set_page_config(page_title="FundCraft", page_icon="📊", layout="wide")

    st.title("📊 FundCraft 全量展示")
    st.caption("基于 Supabase 的基金净值全量展示、指标汇总与同步状态查看")

    configured_passwords = load_access_passwords(PROJECT_ROOT)
    if not configured_passwords:
        st.error("未配置 access_password，请先在 .streamlit/secrets.toml 中设置。")
        st.stop()

    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if not st.session_state["authenticated"]:
        st.subheader("访问验证")
        entered_password = st.text_input("请输入访问口令", type="password")
        if st.button("进入系统", type="primary"):
            if entered_password in configured_passwords:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("口令错误，请重新输入。")
        st.stop()

    if "secret_password" not in st.session_state:
        st.session_state["secret_password"] = ""
    if "data_loaded" not in st.session_state:
        st.session_state["data_loaded"] = False

    with st.sidebar:
        st.header("访问状态")
        st.success("登录口令校验通过")
        st.caption("请再输入解密口令，确认后才会开始读取 Supabase 数据")

        secret_password_input = st.text_input("解密口令", type="password", help="用于解密 secrets.toml 中的 url/key")
        if st.button("读取数据", type="primary"):
            if not secret_password_input.strip():
                st.error("请输入解密口令。")
                st.stop()

            try:
                settings = load_supabase_settings(PROJECT_ROOT, secret_password=secret_password_input)
            except Exception as exc:
                st.error(f"配置读取失败: {exc}")
                st.stop()

            if not supabase_settings_ready(settings):
                st.error("Supabase 配置不完整。")
                st.stop()

            st.session_state["secret_password"] = secret_password_input
            st.session_state["data_loaded"] = True
            st.rerun()

        if not st.session_state["data_loaded"]:
            st.info("输入解密口令并点击“读取数据”后才会开始加载 Supabase 数据。")
            st.stop()

        # ---------- 分析范围（默认近 1 年） ----------
        st.divider()
        st.header("分析范围")
        range_option = st.selectbox(
            "时间范围",
            RANGE_OPTIONS,
            index=RANGE_OPTIONS.index(DEFAULT_RANGE),
            help="默认近 1 年，避免每次全量拉取所有历史数据",
        )
        if range_option == "全部":
            start_date: str | None = None
            end_date: str | None = None
        else:
            years = int(range_option[1])  # "近1年" -> 1
            start_date = (date.today() - timedelta(days=365 * years)).isoformat()
            end_date = date.today().isoformat()

        # ---------- 数据刷新 ----------
        st.divider()
        st.header("数据刷新")
        st.caption("检查各实体同步水位，缺失部分增量补全，并重算派生因子")
        if st.button("刷新数据", type="primary"):
            refresh_secret = str(st.session_state.get("secret_password", "")).strip()
            if not refresh_secret:
                st.error("未获取到解密口令，无法刷新。")
                st.stop()

            with st.spinner("正在同步数据并重算因子..."):
                try:
                    refresh_settings = load_supabase_settings(PROJECT_ROOT, secret_password=refresh_secret)
                    refresh_client = create_supabase_client(refresh_settings)
                    fund_codes = load_fund_codes(PROJECT_ROOT)
                    refresh_results = refresh_with_client(refresh_client, fund_codes)
                    st.session_state["last_refresh_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    st.session_state["last_refresh_results"] = refresh_results
                    load_dashboard_data.clear()
                except Exception as exc:
                    st.error(f"刷新失败: {exc}")
                else:
                    st.success("刷新完成，已重新加载数据。")
                    st.rerun()

        last_refresh = st.session_state.get("last_refresh_time", "暂无")
        st.caption(f"最近刷新：{last_refresh}")

    secret_password = str(st.session_state.get("secret_password", "")).strip()
    if not secret_password:
        st.error("未获取到解密口令。")
        st.stop()

    try:
        settings = load_supabase_settings(PROJECT_ROOT, secret_password=secret_password)
    except Exception as exc:
        st.error(f"配置读取失败: {exc}")
        st.stop()

    if not supabase_settings_ready(settings):
        st.error("Supabase 配置不完整。")
        st.stop()

    try:
        data_bundle = load_dashboard_data(secret_password, start_date=start_date, end_date=end_date)
    except Exception as exc:
        st.error(f"数据加载失败: {exc}")
        st.stop()

    profiles_df = data_bundle["profiles"]
    summary_df = data_bundle["summary"]
    combined_df = data_bundle["combined"]
    latest_sync_df = data_bundle["latest_sync"]
    watermarks_df = data_bundle["watermarks"]

    total_funds = int(len(profiles_df)) if not profiles_df.empty else 0
    total_rows = int(summary_df["row_count"].sum()) if not summary_df.empty else 0
    latest_sync_text = "暂无"
    if not latest_sync_df.empty and "executed_at" in latest_sync_df.columns:
        latest_sync_text = str(latest_sync_df["executed_at"].iloc[0])
    range_text = f"{start_date} ~ {end_date}" if start_date else "全历史"
    last_refresh = st.session_state.get("last_refresh_time", "暂无")

    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
    metric_col1.metric("基金数量", f"{total_funds}")
    metric_col2.metric("窗口内记录数", f"{total_rows}")
    metric_col3.metric("展示时间范围", range_text)
    metric_col4.metric("最近刷新", last_refresh)

    tab_overview, tab_detail, tab_data = st.tabs(["总览", "单基金详情", "数据表"])

    with tab_overview:
        st.subheader("基金收益汇总")
        if summary_df.empty:
            st.warning("没有可展示的基金汇总数据。")
        else:
            chart_col1, chart_col2 = st.columns([1.3, 1])
            with chart_col1:
                st.dataframe(
                    summary_df.assign(
                        cumulative_return_pct=summary_df["cumulative_return_pct"].map(_format_ratio),
                        max_drawdown_pct=summary_df["max_drawdown_pct"].map(_format_ratio),
                        annualized_volatility_pct=summary_df["annualized_volatility_pct"].map(_format_ratio),
                    ),
                    width="stretch",
                    hide_index=True,
                )
            with chart_col2:
                st.plotly_chart(_build_overview_chart(summary_df), width="stretch")

            st.subheader("全量净值与回撤走势")
            st.plotly_chart(_build_nav_and_drawdown_chart(combined_df), width="stretch")

    with tab_detail:
        st.subheader("选择基金查看明细")
        if summary_df.empty or combined_df.empty:
            st.warning("没有可用的基金明细数据。")
        else:
            fund_codes = summary_df["fund_code"].astype(str).tolist()
            selected_fund = st.selectbox("基金代码", fund_codes, index=0)
            fund_summary = summary_df[summary_df["fund_code"] == selected_fund]
            fund_nav = combined_df[combined_df["fund_code"] == selected_fund].sort_values("nav_date")

            if not fund_summary.empty:
                summary_row = fund_summary.iloc[0]
                s1, s2, s3, s4 = st.columns(4)
                s1.metric("起始净值", f"{summary_row['start_unit_nav']:.4f}")
                s2.metric("最新净值", f"{summary_row['end_unit_nav']:.4f}")
                s3.metric("累计收益率", _format_ratio(float(summary_row['cumulative_return_pct'])))
                s4.metric("最大回撤", _format_ratio(float(summary_row['max_drawdown_pct'])))

            st.plotly_chart(_build_nav_and_drawdown_chart(fund_nav.assign(fund_code=selected_fund)), width="stretch")

            preview_df = fund_nav.copy()
            preview_df["nav_date"] = preview_df["nav_date"].dt.date.astype(str)
            st.dataframe(preview_df, width="stretch", height=360, hide_index=True)

            st.download_button(
                label="下载该基金明细 CSV",
                data=preview_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"{selected_fund}_nav.csv",
                mime="text/csv",
            )

    with tab_data:
        st.subheader("原始数据表")
        st.write("基金基础信息")
        st.dataframe(profiles_df, width="stretch", hide_index=True)
        st.write("同步状态")
        st.dataframe(latest_sync_df, width="stretch", hide_index=True)
        st.write("同步水位（每个实体已同步到的最大日期）")
        if watermarks_df.empty:
            st.info("暂无水位记录。点击侧边栏「刷新数据」完成首次同步。")
        else:
            st.dataframe(
                watermarks_df[["entity_type", "entity_code", "last_date", "source", "updated_at"]],
                width="stretch",
                hide_index=True,
            )
