"""真实数据访问层：从 Supabase 读取，接口与模拟层同构（存储契约）。

设计：
- 各页面只依赖本模块的公开接口（get_nav_history / get_all_funds_overview / ...），
  不关心数据从哪来。
- 连接凭据（解密后的 Supabase url/key）存放在 session_state["supabase_settings"]，
  由 dashboard 侧边栏「数据连接」区管理；未连接时公开函数抛 ConnectionError。
- 所有数据库读通过 @st.cache_data(ttl=...) 缓存，避免每次重跑都打库；
  缓存函数只接收可哈希参数（url/key/code/range），不读取 session_state。
- 市场指数 / 沪深300基准 / 服务器状态 / 回测概览：暂无真实数据源，保留模拟并明确标注。
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from src.config import (
    SupabaseSettings,
    load_factor_fund_codes,
    load_fund_categories,
    load_fund_codes,
    load_supabase_settings,
    supabase_settings_ready,
)
from src.storage.supabase_store import (
    create_supabase_client,
    fetch_daily_factors,
    fetch_fund_dividends,
    fetch_nav_history,
    list_fund_profiles,
    list_watermarks,
)


def normalize_fund_code(code) -> str:
    """基金代码规范化（内联，避免在模块导入期拉取抓取层/akshare）。"""
    normalized = str(code).strip()
    return normalized.zfill(6) if normalized.isdigit() and len(normalized) < 6 else normalized


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 时间范围预设（与详情页胶囊一致）
RANGE_OPTIONS = ["近1月", "近3月", "近6月", "近1年", "近3年", "近5年", "全部"]
RANGE_DAYS = {
    "近1月": 30,
    "近3月": 90,
    "近6月": 180,
    "近1年": 365,
    "近3年": 365 * 3,
    "近5年": 365 * 5,
}
_PERIOD_DAYS = {"近1周": 7, "近1月": 30, "近3月": 90, "近6月": 180, "近1年": 365}


# ---------- 基金目录（类别/面板来自配置；名称等从基金档案补齐） ----------
def _build_catalog() -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    for category in load_fund_categories(PROJECT_ROOT).values():
        for code in category.fund_codes:
            code = normalize_fund_code(code)
            catalog[code] = {
                "fund_code": code,
                "fund_name": "",
                "category": category.name,
                "panel": category.panel,
                "fund_type": "",
                "tracking_index": "",
            }
    return catalog


_CATALOG = _build_catalog()


@lru_cache(maxsize=1)
def get_fund_codes() -> list[str]:
    """配置中的基金代码（规范化，去重）。"""
    return list(_CATALOG.keys())


def get_fund_count() -> int:
    return len(get_fund_codes())


# ---------- 连接管理 ----------
def is_connected() -> bool:
    return bool(st.session_state.get("supabase_settings"))


def _credentials() -> tuple[str, str]:
    settings = st.session_state.get("supabase_settings")
    if not settings:
        raise ConnectionError("尚未连接 Supabase，请在侧边栏输入解密口令并连接。")
    return settings.url, settings.key


def connect(secret_password: str) -> str | None:
    """尝试连接 Supabase；成功返回 None，失败返回错误信息。"""
    try:
        settings = load_supabase_settings(PROJECT_ROOT, secret_password=secret_password.strip())
        if not supabase_settings_ready(settings):
            return "Supabase 配置不完整（缺少 url/key）。"
        client = _client_for(settings.url, settings.key)
        # 轻量连通性检查
        client.table("fund_profiles").select("fund_code").limit(1).execute()
        st.session_state["supabase_settings"] = settings
        return None
    except Exception as exc:  # noqa: BLE001
        return f"连接失败：{exc}"


def disconnect() -> None:
    st.session_state.pop("supabase_settings", None)


@st.cache_resource(show_spinner=False)
def _client_for(url: str, key: str):
    return create_supabase_client(SupabaseSettings(url=url, key=key))


def get_client():
    url, key = _credentials()
    return _client_for(url, key)


# ---------- 基础读取（缓存函数只收可哈希参数） ----------
def _range_bounds(range_key: str) -> tuple[str | None, str | None]:
    if range_key in ("全部", ""):
        return None, None
    days = RANGE_DAYS.get(range_key)
    if days is None:
        return None, None
    start = date.today() - timedelta(days=days)
    return start.isoformat(), None


@st.cache_data(ttl=300, show_spinner=False)
def _nav_history(url: str, key: str, code: str, start: str | None, end: str | None) -> pd.DataFrame:
    return fetch_nav_history(_client_for(url, key), code, start_date=start, end_date=end)


@st.cache_data(ttl=300, show_spinner=False)
def _fund_profiles(url: str, key: str) -> pd.DataFrame:
    return list_fund_profiles(_client_for(url, key))


@st.cache_data(ttl=300, show_spinner=False)
def _dividends(url: str, key: str, code: str) -> pd.DataFrame:
    return fetch_fund_dividends(_client_for(url, key), code)


@st.cache_data(ttl=300, show_spinner=False)
def _strategy_factors(url: str, key: str, code: str) -> pd.DataFrame:
    return fetch_daily_factors(_client_for(url, key), code)


@st.cache_data(ttl=120, show_spinner=False)
def _sync_jobs(url: str, key: str) -> pd.DataFrame:
    response = (
        _client_for(url, key)
        .table("sync_jobs")
        .select("log_id, job_name, status, message, row_count, executed_at")
        .order("executed_at", desc=True)
        .limit(20)
        .execute()
    )
    data = response.data or []
    if not data:
        return pd.DataFrame(columns=["log_id", "job_name", "status", "message", "row_count", "executed_at"])
    df = pd.DataFrame(data)
    df["executed_at"] = pd.to_datetime(df["executed_at"], errors="coerce")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _watermarks(url: str, key: str) -> pd.DataFrame:
    return list_watermarks(_client_for(url, key))


# ---------- 对外接口（与存储契约一致） ----------
def get_nav_history(code: str, range_key: str = "近1年") -> pd.DataFrame:
    """某只基金在指定时间范围内的净值明细（升序）。"""
    url, key = _credentials()
    start, end = _range_bounds(range_key)
    return _nav_history(url, key, normalize_fund_code(code), start, end)


def get_latest_nav(code: str) -> dict:
    """某只基金最新净值快照：净值、日期、日涨跌。"""
    url, key = _credentials()
    code = normalize_fund_code(code)
    recent = _nav_history(url, key, code, (date.today() - timedelta(days=90)).isoformat(), None)
    if recent.empty:
        return {"nav_date": None, "unit_nav": None, "daily_return_pct": None}
    last = recent.iloc[-1]
    daily_return = last.get("daily_return")
    return {
        "nav_date": last["nav_date"],
        "unit_nav": float(last["unit_nav"]),
        "daily_return_pct": float(daily_return) if pd.notna(daily_return) else None,
    }


def _compute_period_returns_from(frame: pd.DataFrame, periods: list[str]) -> dict[str, float | None]:
    ordered = frame.sort_values("nav_date").reset_index(drop=True)
    result: dict[str, float | None] = {}
    if ordered.empty:
        return {p: None for p in periods}
    latest = float(ordered["unit_nav"].iloc[-1])
    for label in periods:
        days = _PERIOD_DAYS.get(label)
        if days is None:
            base = float(ordered["unit_nav"].iloc[0])
            result[label] = (latest / base - 1.0) * 100.0 if base else None
            continue
        cutoff = ordered["nav_date"].iloc[-1] - pd.Timedelta(days=days)
        past = ordered[ordered["nav_date"] <= cutoff]
        base = float(past["unit_nav"].iloc[-1]) if not past.empty else float(ordered["unit_nav"].iloc[0])
        result[label] = (latest / base - 1.0) * 100.0 if base else None
    return result


def get_period_returns(code: str, periods: list[str] | None = None) -> dict[str, float | None]:
    """某只基金各时间区间收益率（%）。"""
    periods = periods or ["近1周", "近1月", "近3月", "近6月", "近1年"]
    url, key = _credentials()
    code = normalize_fund_code(code)
    frame = _nav_history(url, key, code, (date.today() - timedelta(days=400)).isoformat(), None)
    return _compute_period_returns_from(frame, periods)


def _meta_from_catalog_and_profiles(code: str, profiles: pd.DataFrame) -> dict:
    meta = dict(_CATALOG.get(code, {"fund_code": code, "fund_name": "", "category": "", "panel": "净值", "fund_type": "", "tracking_index": ""}))
    if profiles is not None and not profiles.empty and "fund_code" in profiles.columns:
        match = profiles[profiles["fund_code"].astype(str) == code]
        if not match.empty:
            row = match.iloc[0]
            meta["fund_name"] = str(row.get("fund_name")) if row.get("fund_name") else meta.get("fund_name", "")
            meta["fund_type"] = str(row.get("fund_type")) if row.get("fund_type") else ""
            meta["tracking_index"] = str(row.get("tracking_index")) if row.get("tracking_index") else ""
    return meta


def get_fund_meta(code: str) -> dict:
    """某只基金的基础信息（名称/类别/面板/类型/跟踪指数）。"""
    code = normalize_fund_code(code)
    profiles = None
    if is_connected():
        try:
            url, key = _credentials()
            profiles = _fund_profiles(url, key)
        except Exception:  # noqa: BLE001
            profiles = None
    return _meta_from_catalog_and_profiles(code, profiles)


@st.cache_data(ttl=300, show_spinner=False)
def _all_funds_overview(url: str, key: str, codes: tuple[str, ...]) -> pd.DataFrame:
    profiles = _fund_profiles(url, key)
    rows = []
    for code in codes:
        code = normalize_fund_code(code)
        meta = _meta_from_catalog_and_profiles(code, profiles)
        frame = _nav_history(url, key, code, (date.today() - timedelta(days=400)).isoformat(), None)
        latest = _compute_latest_from(frame)
        period = _compute_period_returns_from(frame, ["近1周", "近1月", "近3月"])
        rows.append(
            {
                "fund_code": code,
                "fund_name": meta["fund_name"] or code,
                "category": meta["category"],
                "fund_type": meta["fund_type"],
                "latest_nav": latest["unit_nav"],
                "nav_date": latest["nav_date"],
                "daily_change_pct": latest["daily_return_pct"],
                "return_1w": period.get("近1周"),
                "return_1m": period.get("近1月"),
                "return_3m": period.get("近3月"),
            }
        )
    return pd.DataFrame(rows)


def _compute_latest_from(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"nav_date": None, "unit_nav": None, "daily_return_pct": None}
    ordered = frame.sort_values("nav_date").reset_index(drop=True)
    last = ordered.iloc[-1]
    daily_return = last.get("daily_return")
    return {
        "nav_date": last["nav_date"],
        "unit_nav": float(last["unit_nav"]),
        "daily_return_pct": float(daily_return) if pd.notna(daily_return) else None,
    }


def get_all_funds_overview() -> pd.DataFrame:
    """自选列表总览：名称/类别/最新净值/日涨跌/区间收益。"""
    url, key = _credentials()
    return _all_funds_overview(url, key, tuple(get_fund_codes()))


def get_overview_metrics() -> dict:
    """总览页顶部指标（目录/状态信息，不含持仓口径）。"""
    url, key = _credentials()
    codes = tuple(get_fund_codes())
    overview = _all_funds_overview(url, key, codes)
    latest_date = None
    if not overview.empty:
        dates = pd.to_datetime(overview["nav_date"], errors="coerce").dropna()
        if not dates.empty:
            latest_date = dates.max().strftime("%Y-%m-%d")
    strategy_count = sum(1 for code in codes if _CATALOG.get(code, {}).get("panel") == "红利低波")
    jobs = _sync_jobs(url, key)
    last_sync = jobs["executed_at"].max().strftime("%Y-%m-%d %H:%M:%S") if not jobs.empty else "暂无"
    return {
        "fund_count": len(codes),
        "latest_nav_date": latest_date or "—",
        "strategy_fund_count": strategy_count,
        "last_sync": last_sync,
    }


def get_market_indexes() -> list[dict]:
    """市场指数条（模拟参考值，暂无真实数据源）。"""
    return [
        {"name": "上证指数", "value": 3405.12, "change_pct": 0.82},
        {"name": "深证成指", "value": 10892.33, "change_pct": -0.35},
        {"name": "创业板指", "value": 2215.67, "change_pct": 1.12},
        {"name": "沪深300", "value": 3987.55, "change_pct": 0.61},
    ]


@lru_cache(maxsize=1)
def _benchmark_frame() -> pd.DataFrame:
    """沪深300 模拟走势（参考；日期与净值对齐）。"""
    rng = np.random.default_rng(999)
    dates = pd.bdate_range(end=pd.Timestamp(date.today()), periods=730)
    rets = rng.normal(0.0001, 0.012, len(dates))
    value = 4000.0 * np.exp(np.cumsum(rets))
    value = value * (4000.0 / value[0])
    return pd.DataFrame({"nav_date": dates, "benchmark": np.round(value, 2)})


def get_benchmark(range_key: str = "近1年") -> pd.DataFrame:
    """沪深300 基准序列（模拟参考值）。"""
    frame = _benchmark_frame().copy()
    start, _ = _range_bounds(range_key)
    if start is not None:
        frame = frame[frame["nav_date"] >= pd.Timestamp(start)]
    return frame.reset_index(drop=True)


def get_dividends(code: str) -> pd.DataFrame:
    """某只基金的分红记录（按除息日升序）。"""
    url, key = _credentials()
    return _dividends(url, key, normalize_fund_code(code))


def get_strategy_factors(code: str, tail: int | None = None) -> pd.DataFrame:
    """某只基金最近 N 条策略因子（按交易日期降序返回最近在前）。"""
    url, key = _credentials()
    frame = _strategy_factors(url, key, normalize_fund_code(code))
    if frame.empty:
        return frame
    frame = frame.sort_values("trade_date", ascending=False).reset_index(drop=True)
    if tail is not None:
        frame = frame.head(tail)
    return frame


def get_strategy_overview(code: str) -> dict:
    """某只基金最新策略信号概览。"""
    frame = get_strategy_factors(code, tail=1)
    if frame.empty:
        return {}
    row = frame.iloc[0]
    return {
        "trade_date": row["trade_date"],
        "score_a": float(row["score_a"]),
        "signal_a": bool(row["signal_a"]),
        "score_b": float(row["score_b"]),
        "signal_b": bool(row["signal_b"]),
        "dividend_yield": float(row["dividend_yield"]),
        "spread": float(row["spread"]),
    }


def get_backtest_overview() -> dict:
    """回测概览（模拟数值，仅供 UI 展示；真实回测请走 strategy_backtest）。"""
    return {
        "xirr_pct": -4.8,
        "max_drawdown_pct": -9.2,
        "buy_count": 690,
        "period": "2021-03 ~ 2026-08",
        "total_invest": 4140000,
        "latest_value": 3965000,
    }


def get_sync_jobs() -> pd.DataFrame:
    """最近同步任务日志（真实数据）。"""
    url, key = _credentials()
    return _sync_jobs(url, key)


def get_watermarks() -> pd.DataFrame:
    """各实体同步水位（真实数据）。"""
    url, key = _credentials()
    return _watermarks(url, key)


def get_latest_sync_time() -> str:
    """最近一次同步时间字符串。"""
    url, key = _credentials()
    jobs = _sync_jobs(url, key)
    if jobs.empty:
        return "暂无"
    return jobs["executed_at"].max().strftime("%Y-%m-%d %H:%M:%S")


def get_server_status() -> dict:
    """服务器状态（实例信息真实；负载为模拟参考值）。"""
    last_refresh = "暂无"
    try:
        last_refresh = get_latest_sync_time()
    except Exception:  # noqa: BLE001
        pass
    return {
        "cpu_pct": 12.5,
        "mem_pct": 46.2,
        "disk_pct": 33.8,
        "uptime": "—",
        "region": "Supabase",
        "host": "db.supabase.co",
        "last_refresh": last_refresh,
        "db_rows": 24510,
        "tables": 6,
    }


# ---------- 刷新（真实同步编排） ----------
def _clear_watermarks(client) -> None:
    """清空同步水位，强制全量重拉。"""
    client.table("sync_watermarks").delete().neq("entity_type", "").execute()


def _invalidate_caches() -> None:
    """刷新后清理相关缓存，立即展示新数据。"""
    for func in (
        _nav_history,
        _fund_profiles,
        _dividends,
        _strategy_factors,
        _sync_jobs,
        _watermarks,
        _all_funds_overview,
    ):
        try:
            func.clear()
        except Exception:  # noqa: BLE001
            pass


def run_refresh(full: bool = False) -> tuple[list[dict], str | None]:
    """执行一次真实刷新。

    :param full: True 时先清空水位，强制全量重拉原始数据并重算因子。
    :return: (结果列表, 错误信息)；错误信息为 None 表示整体成功（可能含单项 error）。
    """
    try:
        url, key = _credentials()
        client = _client_for(url, key)
        if full:
            _clear_watermarks(client)
        fund_codes = get_fund_codes()
        factor_fund_codes = [normalize_fund_code(code) for code in load_factor_fund_codes(PROJECT_ROOT)]
        # 惰性导入：刷新编排较重（含 akshare 抓取链），避免在页面启动时加载
        from src.storage.strategy_sync_runner import refresh_with_client

        results = refresh_with_client(client, fund_codes, factor_fund_codes=factor_fund_codes)
        _invalidate_caches()
        return results, None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)
