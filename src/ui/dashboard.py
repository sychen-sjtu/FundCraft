from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from src.config import (
    FundCategory,
    load_factor_fund_codes,
    load_fund_categories,
    load_fund_codes,
    load_supabase_settings,
    supabase_settings_ready,
)
from src.fetchers.akshare_fund_nav import normalize_fund_code
from src.indicators.fund_metrics import compute_fund_metrics
from src.storage.strategy_sync_runner import refresh_with_client
from src.storage.supabase_store import (
    create_supabase_client,
    fetch_latest_sync_job,
    fetch_nav_history,
    list_fund_profiles,
    list_watermarks,
)
from src.ui.panels import render_panel

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# UI 时间范围预设（默认近 1 年，避免每次全量拉取）
RANGE_OPTIONS = ["近1年", "近2年", "近3年", "近5年", "全部"]
DEFAULT_RANGE = "近1年"


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


def _render_category_tab(
    client,
    category: "FundCategory",
    summary_df: pd.DataFrame,
    combined_df: pd.DataFrame,
    profiles_df: pd.DataFrame,
) -> None:
    """渲染一个基金类别标签页：类别决定面板，类别内每只基金一个看板。"""
    st.subheader(f"{category.name}")

    category_codes = [normalize_fund_code(code) for code in category.fund_codes]
    cat_summary = summary_df[summary_df["fund_code"].astype(str).isin(category_codes)]
    if cat_summary.empty or combined_df.empty:
        st.info(f"{category.name} 类别暂无数据（请先在侧边栏读取数据）。")
        return

    profiles_by_code: dict[str, pd.Series] = {}
    if not profiles_df.empty and "fund_code" in profiles_df.columns:
        profiles_by_code = {str(row["fund_code"]): row for _, row in profiles_df.iterrows()}

    for fund_code in category_codes:
        fund_code_n = normalize_fund_code(fund_code)
        if fund_code_n not in cat_summary["fund_code"].astype(str).tolist():
            continue

        fund_nav = combined_df[combined_df["fund_code"].astype(str) == fund_code_n].sort_values("nav_date")
        fund_summary = cat_summary[cat_summary["fund_code"].astype(str) == fund_code_n]
        summary_row = fund_summary.iloc[0] if not fund_summary.empty else None
        profile_row = profiles_by_code.get(fund_code_n)
        fund_name = str(profile_row.get("fund_name")).strip() if profile_row is not None and profile_row.get("fund_name") else ""

        with st.expander(f"{fund_name or fund_code_n}（{fund_code_n}）", expanded=True):
            render_panel(category.panel, client, fund_code_n, fund_name, category.name, fund_nav, summary_row, profile_row)


def render_dashboard() -> None:
    st.set_page_config(page_title="FundCraft", page_icon="📊", layout="wide")

    st.title("📊 FundCraft")
    st.caption("基于 Supabase 的基金数据展示与策略分析")

    if "secret_password" not in st.session_state:
        st.session_state["secret_password"] = ""
    if "data_loaded" not in st.session_state:
        st.session_state["data_loaded"] = False

    with st.sidebar:
        st.header("数据访问")
        st.caption("请输入口令后读取 Supabase 数据")

        secret_password_input = st.text_input("请输入口令", type="password", help="用于解密 secrets.toml 中的 url/key")
        if st.button("读取数据", type="primary"):
            if not secret_password_input.strip():
                st.error("请输入口令。")
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
            st.info("请输入口令并点击“读取数据”后才会开始加载 Supabase 数据。")
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
                st.error("未获取到口令，无法刷新。")
                st.stop()

            with st.spinner("正在同步数据并重算因子..."):
                try:
                    refresh_settings = load_supabase_settings(PROJECT_ROOT, secret_password=refresh_secret)
                    refresh_client = create_supabase_client(refresh_settings)
                    fund_codes = load_fund_codes(PROJECT_ROOT)
                    factor_fund_codes = load_factor_fund_codes(PROJECT_ROOT)
                    refresh_results = refresh_with_client(
                        refresh_client,
                        fund_codes,
                        factor_fund_codes=factor_fund_codes,
                    )
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

        last_results = st.session_state.get("last_refresh_results", [])
        if last_results:
            with st.expander("查看本次刷新结果"):
                for item in last_results:
                    if "error" in item or "factor_error" in item:
                        st.error(str(item))
                    else:
                        st.write(str(item))

    secret_password = str(st.session_state.get("secret_password", "")).strip()
    if not secret_password:
        st.error("未获取到口令。")
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
    range_text = f"{start_date} ~ {end_date}" if start_date else "全历史"
    last_refresh = st.session_state.get("last_refresh_time", "暂无")

    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
    metric_col1.metric("基金数量", f"{total_funds}")
    metric_col2.metric("窗口内记录数", f"{total_rows}")
    metric_col3.metric("展示时间范围", range_text)
    metric_col4.metric("最近刷新", last_refresh)

    # 类别面板需要额外读取数据（分红 / 策略因子），共用一个 client
    client = create_supabase_client(settings)

    categories = load_fund_categories(PROJECT_ROOT)
    tab_names = ["总览"] + list(categories.keys()) + ["数据表"]
    tabs = st.tabs(tab_names)

    with tabs[0]:
        st.subheader("基金概览")
        st.caption("各类别基金数量（明细请到对应类别页查看）")
        for name, cat in categories.items():
            st.markdown(f"- **{name}**：{len(cat.fund_codes)} 只基金")

    for index, category in enumerate(categories.values(), start=1):
        with tabs[index]:
            _render_category_tab(client, category, summary_df, combined_df, profiles_df)

    with tabs[-1]:
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
