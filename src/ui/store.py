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

import calendar
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

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
from src.indicators.evaluation import evaluate_fund
from src.storage.supabase_store import (
    _fetch_all_rows,
    create_supabase_client,
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
# 业绩走势（复权净值）自然月区间：近 N 月 = 最新净值日往回 N 个自然月的同一天
_PERIOD_MONTHS = {"近1月": 1, "近3月": 3, "近6月": 6, "近1年": 12, "近3年": 36}


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
    frame = fetch_nav_history(_client_for(url, key), code, start_date=start, end_date=end)
    if not frame.empty and "trade_date" in frame.columns:
        frame = frame.rename(columns={"trade_date": "nav_date"})
    return frame


@st.cache_data(ttl=300, show_spinner=False)
def _fund_profiles(url: str, key: str) -> pd.DataFrame:
    return list_fund_profiles(_client_for(url, key))


@st.cache_data(ttl=300, show_spinner=False)
def _dividends(url: str, key: str, code: str) -> pd.DataFrame:
    return fetch_fund_dividends(_client_for(url, key), code)


@st.cache_data(ttl=300, show_spinner=False)
def _strategy_factors(url: str, key: str, code: str) -> pd.DataFrame:
    """某基金的策略因子 = 其跟踪指数的指数层因子（ER：信号在指数层）。"""
    client = _client_for(url, key)
    mapping = _fetch_all_rows(
        client.table("fund_tracking_index").select("index_code,role").eq("fund_code", normalize_fund_code(code)).eq("role", "strategy")
    )
    if not mapping:
        return pd.DataFrame()
    index_code = mapping[0]["index_code"]
    data = _fetch_all_rows(
        client.table("index_daily_factors")
        .select(
            "index_code, trade_date, dividend_yield, annualized_volatility, max_drawdown, "
            "dividend_yield_percentile, spread, spread_percentile, dy_vol_ratio_percentile, "
            "drawdown_percentile, volatility_percentile, score_a, signal_a, score_b, signal_b"
        )
        .eq("index_code", index_code)
    )
    frame = pd.DataFrame(data) if data else pd.DataFrame()
    if not frame.empty:
        frame["fund_code"] = normalize_fund_code(code)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


@st.cache_data(ttl=120, show_spinner=False)
def _sync_jobs(url: str, key: str) -> pd.DataFrame:
    response = (
        _client_for(url, key)
        .table("sync_job")
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
    # 分红基金（如 008163 每月分红）必须用复权净值，否则区间收益被低估；无复权时回退单位净值
    col = "adjusted_nav" if "adjusted_nav" in ordered.columns and ordered["adjusted_nav"].notna().any() else "unit_nav"
    latest_ts = ordered["nav_date"].iloc[-1]
    latest = float(ordered[col].iloc[-1])
    for label in periods:
        # 月标签（近1月/近3月/...）统一用自然月回推，与详情业绩走势表、官方区间涨幅口径一致
        months = _PERIOD_MONTHS.get(label)
        if months is not None:
            y, m = latest_ts.year, latest_ts.month - months
            y += (m - 1) // 12
            m = (m - 1) % 12 + 1
            d = min(latest_ts.day, calendar.monthrange(y, m)[1])
            cutoff = pd.Timestamp(y, m, d)
        else:
            days = _PERIOD_DAYS.get(label)
            if days is None:
                base = float(ordered[col].iloc[0])
                result[label] = (latest / base - 1.0) * 100.0 if base else None
                continue
            cutoff = latest_ts - pd.Timedelta(days=days)
        past = ordered[ordered["nav_date"] <= cutoff]
        base = float(past[col].iloc[-1]) if not past.empty else float(ordered[col].iloc[0])
        result[label] = (latest / base - 1.0) * 100.0 if base else None
    return result


def _meta_from_catalog_and_profiles(code: str, profiles: pd.DataFrame) -> dict:
    meta = dict(_CATALOG.get(code, {"fund_code": code, "fund_name": "", "category": "", "panel": "净值", "fund_type": "", "tracking_index": ""}))
    if profiles is not None and not profiles.empty and "fund_code" in profiles.columns:
        match = profiles[profiles["fund_code"].astype(str) == code]
        if not match.empty:
            row = match.iloc[0]
            meta["fund_name"] = str(row.get("fund_name")) if row.get("fund_name") else meta.get("fund_name", "")
            meta["fund_type"] = str(row.get("fund_type")) if row.get("fund_type") else ""
            benchmark = row.get("benchmark")
            meta["tracking_index"] = str(benchmark) if benchmark else ""
            meta["benchmark"] = str(benchmark) if benchmark else ""
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
    """市场指数条（真实数据：index_daily_history 最新收盘/涨跌幅）。"""
    if not is_connected():
        return []
    url, key = _credentials()
    return _market_indexes(url, key)


@st.cache_data(ttl=300, show_spinner=False)
def _market_indexes(url: str, key: str) -> list[dict]:
    client = _client_for(url, key)
    result = []
    for code, name in (("000001", "上证指数"), ("000300", "沪深300")):
        rows = _fetch_all_rows(
            client.table("index_daily_history")
            .select("close,change_pct")
            .eq("index_code", code)
            .order("trade_date", desc=True)
            .limit(1)
        )
        if rows:
            row = rows[0]
            result.append({"name": name, "value": float(row["close"]), "change_pct": float(row.get("change_pct") or 0.0)})
    return result


@lru_cache(maxsize=1)
@st.cache_data(ttl=300, show_spinner=False)
def _benchmark_frame(url: str, key: str) -> pd.DataFrame:
    """沪深300 全收益指数（000300S）真实走势，用于归一化对比。"""
    client = _client_for(url, key)
    rows = _fetch_all_rows(
        client.table("index_daily_history").select("trade_date,close").eq("index_code", "000300S")
    )
    frame = pd.DataFrame(rows) if rows else pd.DataFrame()
    if not frame.empty:
        frame = frame.rename(columns={"trade_date": "nav_date", "close": "benchmark"})
        frame["nav_date"] = pd.to_datetime(frame["nav_date"], errors="coerce")
    return frame


def get_benchmark(range_key: str = "近1年") -> pd.DataFrame:
    """沪深300 全收益基准序列（真实数据 000300S）。"""
    url, key = _credentials()
    frame = _benchmark_frame(url, key)
    if frame.empty:
        return frame
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


@st.cache_data(ttl=300, show_spinner=False)
def _fund_evaluation(url: str, key: str, code: str) -> dict:
    """带 client 的评估（缓存 5 分钟；client 来自可哈希的 url/key）。"""
    return evaluate_fund(code, client=_client_for(url, key))


def get_fund_evaluation(code: str) -> dict:
    """对单只基金做渐进式评估：返回三层就绪度（status/missing/hint/layers）。

    数据纪律：只返回真实数据——基金净值/分红/cn_10y 读 Supabase，指数行情/股息率/PE
    读理杏仁导出 CSV；未连接或某层取不到 → 对应层「暂无数据」，绝不模拟。
    """
    code = normalize_fund_code(code)
    if is_connected():
        try:
            url, key = _credentials()
            return _fund_evaluation(url, key, code)
        except Exception:  # noqa: BLE001 - 连接异常 → 回退无 client 评估
            pass
    return evaluate_fund(code)


def get_backtest_overview() -> dict | None:
    """回测概览占位。

    字段保留但功能未接入：真实回测引擎见 src/indicators/strategy_backtest.py，
    接入前返回 None（界面显示「暂无回测数据」）。
    """
    return None


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


@st.cache_data(ttl=300)
def _server_counts(url: str, key: str) -> tuple[int, int]:
    """存储服务器（Supabase）真实数据行数与表数（缓存 5 分钟）。"""
    client = _client_for(url, key)
    main_tables = (
        "fund_nav_history",
        "fund_dividends",
        "index_daily_history",
        "index_daily_factors",
        "index_valuation_history",
        "macro_rates_history",
        "fund_profiles",
        "fund_tracking_index",
        "index_master",
        "sync_job",
        "sync_watermark",
    )
    db_rows = 0
    tables = 0
    for table in main_tables:
        try:
            resp = client.table(table).select("*", count="exact").limit(1).execute()
            count = int(resp.count or 0)
            db_rows += count
            tables += 1 if count > 0 else 0
        except Exception:  # noqa: BLE001 - 单表失败不阻塞整体
            pass
    return db_rows, tables


def get_server_status() -> dict:
    """存储服务器（Supabase）真实状态：数据行数 / 表数 / 实例 / 最近刷新。

    数据纪律：只返回真实值。CPU / 内存 / 磁盘等基础设施指标需 Supabase
    Management API（额外 token），未配置前返回 None → 界面显示「暂无」，不模拟。
    """
    last_refresh = "暂无"
    try:
        last_refresh = get_latest_sync_time()
    except Exception:  # noqa: BLE001
        pass

    host = "Supabase"
    db_rows = 0
    tables = 0
    try:
        url, key = _credentials()
        host = urlparse(url).netloc or "Supabase"
        db_rows, tables = _server_counts(url, key)
    except Exception:  # noqa: BLE001
        pass

    return {
        "cpu_pct": None,
        "mem_pct": None,
        "disk_pct": None,
        "db_rows": db_rows,
        "tables": tables,
        "host": host,
        "last_refresh": last_refresh,
    }


# ---------- 刷新（真实同步编排） ----------
def _clear_watermarks(client) -> None:
    """清空同步水位，强制全量重拉。"""
    client.table("sync_watermark").delete().neq("entity_type", "").execute()


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
        # 惰性导入：刷新编排较重（含 akshare 抓取链），避免在页面启动时加载
        from src.storage.strategy_sync_runner import refresh_all

        results = refresh_all(client)
        _invalidate_caches()
        return results, None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


# ---------- 数据刷新（分层设计） ----------

# 数据分层定义：UI 展示 + 按层刷新分发
REFRESH_LAYERS: tuple[dict, ...] = (
    {"key": "fund", "label": "基金层", "icon": "📈", "desc": "净值 / 分红 / 档案"},
    {"key": "index", "label": "指数层", "icon": "📊", "desc": "指数价格 / 估值"},
    {"key": "rate", "label": "宏观层", "icon": "🏦", "desc": "cn_10y 利率"},
    {"key": "factors", "label": "策略层", "icon": "🧮", "desc": "派生因子（随底层重算）"},
)


def get_refresh_layers() -> list[dict]:
    """返回各数据层的刷新状态（更新至日期 + 新鲜度）。

    数据纪律：更新至日期取自真实同步水位；未接入 / 暂无的层显示「暂无」。
    """
    last: dict[str, pd.Timestamp | None] = {}
    try:
        url, key = _credentials()
        wm = _watermarks(url, key)
        if not wm.empty:
            wm = wm.copy()
            wm["last_date"] = pd.to_datetime(wm["last_date"], errors="coerce")
            for layer in REFRESH_LAYERS:
                k = layer["key"]
                if k == "fund":
                    sub = wm[wm["entity_type"] == "fund"]
                    last[k] = sub["last_date"].max() if not sub.empty else None
                elif k == "index":
                    sub = wm[wm["entity_type"] == "index"]
                    last[k] = sub["last_date"].max() if not sub.empty else None
                elif k == "rate":
                    sub = wm[wm["entity_type"] == "rate"]
                    last[k] = sub["last_date"].max() if not sub.empty else None
                elif k == "factors":
                    last[k] = None  # 随底层重算
    except Exception:  # noqa: BLE001 - 未连接 / 水位读取失败 → 各层显示暂无
        pass

    today = pd.Timestamp.today().normalize()
    layers: list[dict] = []
    for layer in REFRESH_LAYERS:
        k = layer["key"]
        d = last.get(k)
        if d is None:
            status = "pending"
            last_text = "暂无"
        else:
            days = (today - pd.Timestamp(d).normalize()).days
            status = "fresh" if days <= 3 else "stale"
            last_text = pd.Timestamp(d).date().isoformat()
        layers.append({**layer, "last_updated": last_text, "status": status})
    return layers


def run_layer_refresh(layer_key: str) -> tuple[list[dict], str | None]:
    """按层刷新（真实同步编排：fund/index/rate/factors 走 strategy_sync_runner.refresh_layer）。

    - fund / index / rate / factors → refresh_layer（拉取 + 水位）
    """
    layer = next((x for x in REFRESH_LAYERS if x["key"] == layer_key), None)
    label = f'{layer["icon"]} {layer["label"]}' if layer else layer_key
    try:
        url, key = _credentials()
        client = _client_for(url, key)
        # 惰性导入：刷新编排较重（含 akshare 抓取链），避免在页面启动时加载
        from src.storage.strategy_sync_runner import refresh_layer

        results, error = refresh_layer(client, layer_key)
        _invalidate_caches()
        return results, error or "完成"
    except Exception as exc:  # noqa: BLE001
        return [], f"{label}按层刷新失败：{exc}"
