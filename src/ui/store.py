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
import re
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import streamlit as st

from src.config import (
    BOND_SIGNAL_PANELS,
    FundCategory,
    SupabaseSettings,
    load_supabase_settings,
    supabase_settings_ready,
)
from src.indicators.evaluation import evaluate_fund
from src.indicators.fund_metrics import build_drawdown_series
from src.storage.fund_config import (
    add_member,
    categories_from_config,
    delete_category,
    fetch_fund_config,
    next_sort_order,
    remove_member,
    reorder_categories,
    reorder_members,
    upsert_category,
)
from src.storage.supabase_store import (
    _execute_with_retry,
    _fetch_all_rows,
    create_supabase_client,
    fetch_fund_dividends,
    fetch_fund_snapshot_metrics,
    fetch_macro_rates,
    fetch_nav_history,
    list_fund_profiles,
    list_watermarks,
    upsert_fund_snapshot_metrics,
)
from src.storage.ui_index_config import (
    LIST_MARKET_INDEXES,
    add_index,
    fetch_index_list,
    remove_index,
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

# 数据缓存有效期（秒）。业务数据只在「数据管理」页刷新时变化，而刷新会显式清理缓存
# （见 _invalidate_caches），因此 TTL 取长值即可 —— 短 TTL 只会让每次重开页面都重新
# 走一遍全部网络请求（实测冷启动 ~68s / 18 次请求，缓存命中 0s / 0 次）。
# 若数据在别处（另一台机器 / 命令行）更新过，点「🗄️ 数据管理」页的
# 「🔄 重新读取服务器数据」强制刷新即可。
_CACHE_TTL = 12 * 3600


# ---------- 基金目录（类别/面板来自配置；名称等从基金档案补齐） ----------
def _build_catalog(categories: dict[str, FundCategory]) -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    for category in categories.values():
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


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _fund_config(url: str, key: str) -> dict:
    """库表配置（类别 + 成员）；配置表未建时 available=False。"""
    return fetch_fund_config(_client_for(url, key))


def get_fund_categories() -> dict[str, FundCategory]:
    """当前生效的基金类别配置（库表是唯一来源）。

    - 配置表可用 → 以库表为准（网页端增删即生效；删空就是没有基金）。
    - 配置表未创建 / 数据库不可达 / 未连接 → 空配置。

    不读任何文件配置：没有库表或连不上库，就是「没有基金」。
    读取异常在此吞掉并返回空（表现为「暂无自选基金」，而不是整页报错）。
    """
    if not is_connected():
        return {}
    try:
        url, key = _credentials()
        raw = _fund_config(url, key)  # 走 5 分钟缓存，避免每次交互都多打一次数据库
    except Exception:  # noqa: BLE001 - 网络/权限异常 → 视为没有基金
        return {}
    if not raw.get("available"):
        return {}  # 配置表还没建 → 没有基金
    return categories_from_config(raw)


def get_fund_catalog() -> dict[str, dict]:
    """基金目录（基金代码 → 类别/展示面板等元数据）。"""
    return _build_catalog(get_fund_categories())


def get_fund_codes() -> list[str]:
    """配置中的基金代码（规范化，去重）。"""
    return list(get_fund_catalog().keys())


# ---------- 基金配置管理（网页端增删「需要关注的基金」） ----------
# 写操作一律落库（fund_category / fund_category_member），随后清理缓存使页面立即生效。
# 建表脚本：sql/schema.sql（配置域两张表）；运行期库表是唯一来源，不读任何文件配置。
# 一次性迁移（把 TOML 配置灌入库）走命令行：
#     python scripts/check_fund_config.py --seed
def _snapshot_items(
    categories: dict[str, FundCategory],
    members_by_category: dict[str, list[dict]],
) -> list[dict]:
    return [
        {"name": name, "panel": category.panel, "members": members_by_category.get(name, [])}
        for name, category in categories.items()
    ]


def get_fund_config_snapshot() -> dict:
    """配置页数据：类别 / 成员明细 + 数据来源。

    :return: ``{"source": "db"|"missing", "categories": [...]}``

    - ``db``      ：配置表可用（库表是唯一来源；categories 为空 = 还没配置任何基金）
    - ``missing`` ：配置表尚未创建（需先执行 sql/schema.sql）

    数据库不可达时直接抛异常，由页面提示「读取失败」——此时无法管理基金。
    """
    url, key = _credentials()
    raw = _fund_config(url, key)
    if not raw.get("available"):
        return {"source": "missing", "categories": []}

    members_by_category: dict[str, list[dict]] = {}
    for row in raw.get("members") or []:
        name = str(row.get("category_name", "")).strip()
        members_by_category.setdefault(name, []).append(
            {
                "fund_code": normalize_fund_code(row.get("fund_code", "")),
                "index_code": str(row.get("index_code") or "").strip(),
            }
        )
    return {
        "source": "db",
        "categories": _snapshot_items(categories_from_config(raw), members_by_category),
    }


def create_fund_category(name: str, panel: str) -> None:
    """新建基金类别（sort_order 追加到现有类别之后）。"""
    url, key = _credentials()
    client = _client_for(url, key)
    upsert_category(client, name, panel, sort_order=next_sort_order(client))
    _invalidate_caches()


def delete_fund_category(name: str) -> None:
    """删除基金类别（成员随外键 cascade 删除）。"""
    url, key = _credentials()
    delete_category(_client_for(url, key), name)
    _invalidate_caches()


def add_fund(category_name: str, fund_code: str, index_code: str = "") -> None:
    """把一只基金加入类别（= 加入关注列表）。"""
    url, key = _credentials()
    add_member(_client_for(url, key), category_name, fund_code, index_code)
    _invalidate_caches()


def remove_fund(category_name: str, fund_code: str) -> None:
    """把一只基金移出类别（= 取消关注）。"""
    url, key = _credentials()
    remove_member(_client_for(url, key), category_name, fund_code)
    _invalidate_caches()


def move_category(name: str, delta: int) -> None:
    """类别整体上移/下移一位（delta = -1 上移 / +1 下移）；已在边界则不动。

    顺序即总览页分组顺序与「基金配置」页的排列顺序。
    """
    url, key = _credentials()
    client = _client_for(url, key)
    names = [item["name"] for item in get_fund_config_snapshot()["categories"]]
    if name not in names:
        return
    index = names.index(name)
    target = index + delta
    if target < 0 or target >= len(names):
        return
    names[index], names[target] = names[target], names[index]
    reorder_categories(client, names)
    _invalidate_caches()


def move_fund(category_name: str, fund_code: str, delta: int) -> None:
    """类别内某只基金上移/下移一位（delta = -1 上移 / +1 下移）；已在边界则不动。

    顺序即总览页该类别下基金卡片的排列顺序。
    """
    url, key = _credentials()
    client = _client_for(url, key)
    snapshot = get_fund_config_snapshot()
    category = next((item for item in snapshot["categories"] if item["name"] == category_name), None)
    if category is None:
        return
    codes = [member["fund_code"] for member in category["members"]]
    code = normalize_fund_code(fund_code)
    if code not in codes:
        return
    index = codes.index(code)
    target = index + delta
    if target < 0 or target >= len(codes):
        return
    codes[index], codes[target] = codes[target], codes[index]
    reorder_members(client, category_name, codes)
    _invalidate_caches()


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


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _nav_history(url: str, key: str, code: str, start: str | None, end: str | None) -> pd.DataFrame:
    frame = fetch_nav_history(_client_for(url, key), code, start_date=start, end_date=end)
    if not frame.empty and "trade_date" in frame.columns:
        frame = frame.rename(columns={"trade_date": "nav_date"})
    return frame


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _fund_profiles(url: str, key: str) -> pd.DataFrame:
    return list_fund_profiles(_client_for(url, key))


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _dividends(url: str, key: str, code: str) -> pd.DataFrame:
    return fetch_fund_dividends(_client_for(url, key), code)


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _strategy_factors(url: str, key: str, code: str) -> pd.DataFrame:
    """某基金的策略因子 = 其跟踪指数的指数层因子（ER：信号在指数层）。

    用途说明：「策略指标」可视化已下线，本表仍被 RSI 动能看板复用
    （index_daily_factors.spread 用于股息率利差线），因此保留读取与入库链路。
    """
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


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _sync_jobs(url: str, key: str) -> pd.DataFrame:
    """最近同步任务日志（TTL=_CACHE_TTL；刷新后由 _invalidate_caches 主动清除）。"""
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


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
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


def _attach_cumulative_nav(frame: pd.DataFrame, dividends: pd.DataFrame) -> pd.DataFrame:
    """在净值明细上追加累计净值列（= 单位净值 + 截至当日的累计每份分红，官方口径）。

    累计净值（现金分红未再投资）与天天基金口径一致，全部来自真实数据；分红为空时等于单位净值。
    """
    nav = frame.sort_values("nav_date").reset_index(drop=True).copy()
    nav["nav_date"] = pd.to_datetime(nav["nav_date"], errors="coerce")
    unit = nav["unit_nav"].astype(float)
    if dividends is None or dividends.empty or "ex_date" not in dividends.columns:
        nav["cumulative_nav"] = unit
        return nav
    div = dividends.copy()
    div["ex_date"] = pd.to_datetime(div["ex_date"], errors="coerce")
    div = div.dropna(subset=["ex_date", "dividend_per_unit"])
    if div.empty:
        nav["cumulative_nav"] = unit
        return nav
    div_agg = div.groupby("ex_date")["dividend_per_unit"].sum().sort_index()
    div_dates = div_agg.index.values
    div_vals = div_agg.values
    nav_dates = nav["nav_date"].values
    # 截至每个净值日的累计每份分红（ex_date <= nav_date），searchsorted 避免重复累加
    idx = np.searchsorted(div_dates, nav_dates, side="right")
    cum_div = np.concatenate([[0.0], np.cumsum(div_vals)])[idx]
    nav["cumulative_nav"] = cum_div + unit.values
    return nav


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _nav_with_cumulative(url: str, key: str, code: str, start: str | None, end: str | None) -> pd.DataFrame:
    """净值明细（含累计净值列）：复用净值/分红缓存，内存推导累计净值。"""
    frame = _nav_history(url, key, code, start, end)
    if frame.empty:
        return frame
    return _attach_cumulative_nav(frame, _dividends(url, key, code))


def get_nav_history_with_cumulative(code: str, range_key: str = "近1年") -> pd.DataFrame:
    """某只基金指定范围内的净值明细（含累计净值列），供详情页历史净值表使用。"""
    url, key = _credentials()
    start, end = _range_bounds(range_key)
    return _nav_with_cumulative(url, key, normalize_fund_code(code), start, end)


# 快照指标新鲜度（秒）：基金规模日更 → 24h；债券持仓季度更 → 7 天；
# nav 派生指标（年化/回撤/卡玛/年限/回撤修复）随净值日更 → 24h
_SCALE_TTL_SECONDS = 24 * 3600
_HOLDINGS_TTL_SECONDS = 7 * 24 * 3600
_METRICS_TTL_SECONDS = 24 * 3600


def _row_fresh(row: dict, ttl_seconds: int, ts_col: str = "updated_at") -> bool:
    """快照指标是否新鲜（距抓取时间 < ttl 秒）；时间戳缺失/解析失败视为不新鲜。"""
    ts = row.get(ts_col) or row.get("updated_at")
    if not ts:
        return False
    try:
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(_BJ_TZ)
        else:
            stamp = stamp.tz_convert(_BJ_TZ)
        return (datetime.now(_BJ_TZ) - stamp).total_seconds() < ttl_seconds
    except Exception:  # noqa: BLE001
        return False


def _fetch_scale_akshare(code: str) -> float | None:
    """基金最新规模（亿元）：akshare 雪球档案；失败返回 None。"""
    try:
        import akshare as ak
        import re

        df = ak.fund_individual_basic_info_xq(symbol=code)
        if df is None or df.empty or "item" not in df.columns or "value" not in df.columns:
            return None
        info = dict(zip(df["item"].astype(str), df["value"].astype(str)))
        for key in ("最新规模", "规模"):
            raw = str(info.get(key, "") or "").strip()
            if not raw:
                continue
            # 形如 "45.19亿" / "1.23亿元" / "456.78万" / "1.23万元"
            match = re.search(r"([\d.]+)\s*(亿|万)(?:元)?", raw.replace(",", ""))
            if match:
                value = float(match.group(1))
                return value if match.group(2) == "亿" else value / 10000.0
        return None
    except Exception:  # noqa: BLE001
        return None


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _fund_scale(url: str, key: str, code: str) -> float | None:
    """基金最新规模（亿元）。优先读 Supabase fund_snapshot_metrics（24h 内新鲜），
    否则调 akshare 雪球并回写（低频变化，持久化后冷缓存也秒开）。"""
    code = normalize_fund_code(code)
    client = None
    try:
        client = _client_for(url, key)
        row = fetch_fund_snapshot_metrics(client, code)
        if row is not None and row.get("fund_scale") is not None and _row_fresh(row, _SCALE_TTL_SECONDS, "scale_updated_at"):
            return float(row["fund_scale"])
    except Exception:  # noqa: BLE001 - 快照表未建/读取失败 → 回退 akshare
        pass
    scale = _fetch_scale_akshare(code)
    if scale is not None and client is not None:
        try:
            upsert_fund_snapshot_metrics(
                client,
                {"fund_code": code, "fund_scale": scale, "scale_updated_at": datetime.now(_BJ_TZ).isoformat()},
            )
        except Exception:  # noqa: BLE001 - 回写失败不影响展示
            pass
    return scale


def get_fund_bond_metrics(code: str) -> dict:
    """固收+/债基 核心指标（供详情页与对比表）。年化/回撤/卡玛/年限来自复权净值，
    规模来自 fund_snapshot_metrics（持久化，冷缓存也快）。"""
    url, key = _credentials()
    return _fund_bond_metrics(url, key, normalize_fund_code(code))


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _fund_bond_metrics(url: str, key: str, code: str) -> dict:
    """固收+ 核心指标：历史年化收益 / 最大回撤 / 卡玛比率 / 基金年限 / 基金规模（亿元）。

    优先读快照表 fund_metrics（24h 内新鲜）→ 冷缓存也直接读库，不再每次重启拉全历史净值；
    否则从全历史复权净值计算并回写。拿不到 → None（界面显示「暂无」，绝不模拟）。
    """
    result = {
        "annualized_return": None,
        "max_drawdown": None,
        "calmar_ratio": None,
        "fund_age_years": None,
        "fund_scale": None,
        "inception_year": None,
    }
    code = normalize_fund_code(code)
    client = None
    try:
        client = _client_for(url, key)
        row = fetch_fund_snapshot_metrics(client, code)
        if row is not None and row.get("fund_metrics") and _row_fresh(row, _METRICS_TTL_SECONDS, "fund_metrics_updated_at"):
            return {**result, **row["fund_metrics"]}
    except Exception:  # noqa: BLE001 - 快照表未建/读取失败 → 回退计算
        pass
    try:
        nav = _nav_history(url, key, code, None, None)
        if nav is not None and not nav.empty:
            ordered = nav.sort_values("nav_date").reset_index(drop=True)
            col = "adjusted_nav" if "adjusted_nav" in ordered.columns and ordered["adjusted_nav"].notna().any() else "unit_nav"
            first = pd.Timestamp(ordered["nav_date"].iloc[0])
            last = pd.Timestamp(ordered["nav_date"].iloc[-1])
            span_days = max((last - first).days, 1)
            result["fund_age_years"] = span_days / 365.25
            result["inception_year"] = first.year  # 年化收益的起算年份（标注用）

            base = float(ordered[col].iloc[0])
            end = float(ordered[col].iloc[-1])
            if base and end > 0:
                result["annualized_return"] = ((end / base) ** (365.25 / span_days) - 1.0) * 100.0

            dd = build_drawdown_series(ordered, nav_col=col)
            if not dd.empty and "drawdown_pct" in dd.columns:
                result["max_drawdown"] = float(dd["drawdown_pct"].min())

            if result["annualized_return"] is not None and result["max_drawdown"] is not None and result["max_drawdown"] < 0:
                result["calmar_ratio"] = result["annualized_return"] / abs(result["max_drawdown"])

        result["fund_scale"] = _fund_scale(url, key, code)
    except Exception:  # noqa: BLE001 - 单基金指标失败不阻塞页面
        pass
    if client is not None:
        try:
            upsert_fund_snapshot_metrics(
                client,
                {"fund_code": code, "fund_metrics": result, "fund_metrics_updated_at": datetime.now(_BJ_TZ).isoformat()},
            )
        except Exception:  # noqa: BLE001 - 回写失败不影响展示
            pass
    return result


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _funds_bond_comparison(url: str, key: str, codes: tuple[str, ...]) -> pd.DataFrame:
    """固收+ 核心指标对比（缓存 30 分钟）。批量读快照行：新鲜则直接重建（冷缓存也秒开）。"""
    overview = _all_funds_overview(url, key, tuple(codes))
    by_code = {str(r.fund_code): r for r in overview.itertuples(index=False)}
    snapshot: dict[str, dict] = {}
    try:
        client = _client_for(url, key)
        data = _fetch_all_rows(
            client.table("fund_snapshot_metrics")
            .select("*")
            .in_("fund_code", [normalize_fund_code(c) for c in codes])
        )
        snapshot = {normalize_fund_code(str(r["fund_code"])): r for r in data}
    except Exception:  # noqa: BLE001 - 快照表未建/读取失败 → 走单只
        pass
    rows = []
    for code in codes:
        code = normalize_fund_code(code)
        row = snapshot.get(code)
        if row is not None and row.get("fund_metrics") and _row_fresh(row, _METRICS_TTL_SECONDS, "fund_metrics_updated_at"):
            m = {
                "annualized_return": None, "max_drawdown": None, "calmar_ratio": None,
                "fund_age_years": None, "fund_scale": None, "inception_year": None,
                **row["fund_metrics"],
            }
        else:
            m = _fund_bond_metrics(url, key, code)
        ov = by_code.get(code)
        rows.append(
            {
                "fund_code": code,
                "fund_name": str(ov.fund_name) if ov is not None else "",
                "annualized_return": m["annualized_return"],
                "inception_year": m["inception_year"],
                "max_drawdown": m["max_drawdown"],
                "calmar_ratio": m["calmar_ratio"],
                "fund_scale": m["fund_scale"],
                "fund_age_years": m["fund_age_years"],
                "return_1m": getattr(ov, "return_1m", None) if ov is not None else None,
                "return_3m": getattr(ov, "return_3m", None) if ov is not None else None,
            }
        )
    return pd.DataFrame(rows)


def get_funds_bond_comparison(codes: list[str]) -> pd.DataFrame:
    """固收+ 基金核心指标对比（历史年化/近1月/近3月/最大回撤/卡玛/年限/规模）。"""
    url, key = _credentials()
    return _funds_bond_comparison(url, key, tuple(normalize_fund_code(c) for c in codes))


# ---------- 债基 风控与持仓（panel=债基，如 007171） ----------
def _drawdown_recoveries(ordered: pd.DataFrame, col: str) -> list[dict]:
    """已收复回撤段列表：从峰值滑落到重新创新高的交易日跨度。

    :return: [{start, end, days, max_dd}]，days=交易日数（收复=净值回到前峰），max_dd=该段最深回撤(%)。
    只统计「已收复」的回撤段；当前仍处于回撤中（未收复）不计入。
    """
    nav = pd.to_numeric(ordered[col], errors="coerce").to_numpy(dtype=float)
    dates = pd.to_datetime(ordered["nav_date"], errors="coerce").to_numpy()
    n = len(nav)
    peak_idx = 0
    start_idx: int | None = None
    depth_min = 0.0
    recoveries: list[dict] = []
    for i in range(n):
        if not np.isfinite(nav[i]):
            continue
        if nav[i] >= nav[peak_idx]:
            if start_idx is not None:
                recoveries.append(
                    {
                        "start": dates[start_idx],
                        "end": dates[i],
                        "days": i - start_idx,
                        "max_dd": depth_min,
                    }
                )
                start_idx = None
            peak_idx = i
            depth_min = 0.0
        else:
            if start_idx is None:
                start_idx = peak_idx
                depth_min = (nav[i] / nav[peak_idx] - 1.0) * 100.0
            else:
                depth_min = min(depth_min, (nav[i] / nav[peak_idx] - 1.0) * 100.0)
    return recoveries


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _bond_risk_metrics(url: str, key: str, code: str) -> dict:
    """债基风控指标：近1年/全历史最大回撤 + 最长已收复回撤段（交易日）。

    优先读快照表 bond_metrics（24h 内新鲜）→ 冷缓存也直接读库；否则从全历史复权净值
    计算并回写。全部由落库复权净值派生（真实数据，无模拟）；拿不到 → None。
    """
    code = normalize_fund_code(code)
    result = {"max_dd_1y": None, "max_dd_all": None, "recover_1y": None, "recover_all": None}
    client = None
    try:
        client = _client_for(url, key)
        row = fetch_fund_snapshot_metrics(client, code)
        if row is not None and row.get("bond_metrics") and _row_fresh(row, _METRICS_TTL_SECONDS, "bond_metrics_updated_at"):
            return {**result, **row["bond_metrics"]}
    except Exception:  # noqa: BLE001 - 快照表未建/读取失败 → 回退计算
        pass
    nav = _nav_history(url, key, code, None, None)
    if nav is None or nav.empty:
        return result
    if "trade_date" in nav.columns:
        nav = nav.rename(columns={"trade_date": "nav_date"})
    ordered = nav.sort_values("nav_date").reset_index(drop=True).copy()
    col = "adjusted_nav" if "adjusted_nav" in ordered.columns and ordered["adjusted_nav"].notna().any() else "unit_nav"
    ordered[col] = pd.to_numeric(ordered[col], errors="coerce")
    ordered = ordered.dropna(subset=["nav_date", col])
    if ordered.empty:
        return result
    dd = build_drawdown_series(ordered, nav_col=col)
    if dd.empty or "drawdown_pct" not in dd.columns:
        return result
    result["max_dd_all"] = float(dd["drawdown_pct"].min())
    cutoff = pd.Timestamp(ordered["nav_date"].iloc[-1]) - pd.Timedelta(days=365)
    y1 = dd[pd.to_datetime(dd["nav_date"]) >= cutoff]
    if not y1.empty:
        result["max_dd_1y"] = float(y1["drawdown_pct"].min())
    rec = _drawdown_recoveries(ordered, col)
    if rec:
        result["recover_all"] = max(rec, key=lambda r: r["days"])
        y1_rec = [r for r in rec if pd.Timestamp(r["end"]) >= cutoff]
        if y1_rec:
            result["recover_1y"] = max(y1_rec, key=lambda r: r["days"])
    if client is not None and (result["max_dd_all"] is not None or result["recover_all"] is not None):
        try:
            # 持久化：dict 内 Timestamp/np.datetime64/np.float64 → JSON 可序列化
            def _jsonable(v):
                if isinstance(v, dict):
                    return {k: _jsonable(x) for k, x in v.items()}
                if isinstance(v, pd.Timestamp) or isinstance(v, np.datetime64):
                    return pd.Timestamp(v).isoformat()
                if isinstance(v, np.floating):
                    return float(v)
                if isinstance(v, np.integer):
                    return int(v)
                return v

            upsert_fund_snapshot_metrics(
                client,
                {"fund_code": code, "bond_metrics": _jsonable(result), "bond_metrics_updated_at": datetime.now(_BJ_TZ).isoformat()},
            )
        except Exception:  # noqa: BLE001 - 回写失败不影响展示
            pass
    return result


_BOND_CATEGORY_RULES = (
    ("国债", "国债"),
    ("国开", "国开债"),
    ("进出", "政金债"),
    ("农发", "政金债"),
    ("转债", "可转债"),
)


def _bond_category(name: str) -> str:
    """按债券名称前缀归类（接口无显式类型字段，靠名称推断；其余归信用债）。"""
    for keyword, label in _BOND_CATEGORY_RULES:
        if keyword in name:
            return label
    return "信用债"


def _quarter_key(q: str) -> tuple[int, int]:
    m = re.search(r"(\d{4})年(\d+)季度", str(q))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _fetch_holdings_akshare(code: str) -> dict:
    """债基底层安全性：akshare 最新报告期债券持仓按类别归类。拿不到 → {}。

    :return: {report_period, categories(相对占比), nav_pct(占净值), total_nav_pct,
              count, no_stock, has_convertible}。
    """
    try:
        import akshare as ak
    except Exception:  # noqa: BLE001
        return {}
    df = None
    for year in (date.today().year, date.today().year - 1, date.today().year - 2):
        try:
            candidate = ak.fund_portfolio_bond_hold_em(symbol=code, date=str(year))
        except Exception:  # noqa: BLE001
            candidate = None
        if candidate is not None and not candidate.empty and "季度" in candidate.columns:
            df = candidate
            break
    if df is None:
        return {}
    latest_q = max(df["季度"].dropna().unique(), key=_quarter_key)
    sub = df[df["季度"] == latest_q].copy()
    sub["占净值比例"] = pd.to_numeric(sub["占净值比例"], errors="coerce").fillna(0.0)
    total = float(sub["占净值比例"].sum())
    cats: dict[str, float] = {}
    for _, r in sub.iterrows():
        label = _bond_category(str(r["债券名称"]))
        cats[label] = cats.get(label, 0.0) + float(r["占净值比例"])
    order = sorted(cats.items(), key=lambda kv: -kv[1])
    categories = [(label, (v / total * 100.0) if total else 0.0) for label, v in order]
    # 股票持仓（空 → 不含股票）
    no_stock = None
    try:
        year_str = re.search(r"(\d{4})", str(latest_q))
        stocks = ak.fund_portfolio_hold_em(symbol=code, date=year_str.group(1)) if year_str else None
        no_stock = bool(stocks is None or stocks.empty)
    except Exception:  # noqa: BLE001
        no_stock = None
    return {
        "report_period": str(latest_q).replace("债券投资明细", "").strip(),
        "categories": [(c, round(v, 1)) for c, v in categories],
        "nav_pct": [(c, round(v, 2)) for c, v in order],
        "total_nav_pct": round(total, 2),
        "count": int(len(sub)),
        "no_stock": no_stock,
        "has_convertible": "可转债" in cats,
    }


def _holdings_from_row(row: dict) -> dict:
    """从快照行重建债基持仓 dict（对应 _fetch_holdings_akshare 返回结构）。"""
    return {
        "report_period": row.get("bond_report_period"),
        "categories": [(c, v) for c, v in (row.get("bond_categories") or [])],
        "nav_pct": [(c, v) for c, v in (row.get("bond_nav_pct") or [])],
        "total_nav_pct": float(row["bond_total_nav_pct"]) if row.get("bond_total_nav_pct") is not None else 0.0,
        "count": row.get("bond_count"),
        "no_stock": row.get("bond_no_stock"),
        "has_convertible": row.get("bond_has_convertible"),
    }


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _bond_holdings_profile(url: str, key: str, code: str) -> dict:
    """债基底层安全性：优先读 Supabase fund_snapshot_metrics（7 天内新鲜），
    否则调 akshare 东财并回写（持仓季度更，持久化后冷缓存也秒开）。"""
    code = normalize_fund_code(code)
    client = None
    try:
        client = _client_for(url, key)
        row = fetch_fund_snapshot_metrics(client, code)
        if row is not None and row.get("bond_report_period") and _row_fresh(row, _HOLDINGS_TTL_SECONDS, "holdings_updated_at"):
            return _holdings_from_row(row)
    except Exception:  # noqa: BLE001 - 快照表未建/读取失败 → 回退 akshare
        pass
    hp = _fetch_holdings_akshare(code)
    if hp and client is not None:
        try:
            upsert_fund_snapshot_metrics(
                client,
                {
                    "fund_code": code,
                    "bond_report_period": hp.get("report_period"),
                    "bond_categories": [list(x) for x in hp.get("categories", [])],
                    "bond_nav_pct": [list(x) for x in hp.get("nav_pct", [])],
                    "bond_total_nav_pct": hp.get("total_nav_pct"),
                    "bond_count": hp.get("count"),
                    "bond_no_stock": hp.get("no_stock"),
                    "bond_has_convertible": hp.get("has_convertible"),
                    "holdings_updated_at": datetime.now(_BJ_TZ).isoformat(),
                },
            )
        except Exception:  # noqa: BLE001 - 回写失败不影响展示
            pass
    return hp


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _bond_risk_comparison(url: str, key: str, codes: tuple[str, ...]) -> pd.DataFrame:
    """债基对比表（缓存 30 分钟）。批量读快照行：新鲜则直接重建（冷缓存也秒开），
    仅未命中/过期才走单只计算+回写。"""
    overview = _all_funds_overview(url, key, tuple(codes))
    by_code = {str(r.fund_code): r for r in overview.itertuples(index=False)}
    snapshot: dict[str, dict] = {}
    try:
        client = _client_for(url, key)
        data = _fetch_all_rows(
            client.table("fund_snapshot_metrics")
            .select("*")
            .in_("fund_code", [normalize_fund_code(c) for c in codes])
        )
        snapshot = {normalize_fund_code(str(r["fund_code"])): r for r in data}
    except Exception:  # noqa: BLE001 - 快照表未建/读取失败 → 走单只
        pass
    rows = []
    for code in codes:
        code = normalize_fund_code(code)
        row = snapshot.get(code)
        if row is not None and row.get("bond_metrics") and _row_fresh(row, _METRICS_TTL_SECONDS, "bond_metrics_updated_at"):
            rm = {"max_dd_1y": None, "max_dd_all": None, "recover_1y": None, "recover_all": None, **row["bond_metrics"]}
            hp = _holdings_from_row(row)
        else:
            rm = _bond_risk_metrics(url, key, code)
            hp = _bond_holdings_profile(url, key, code)
        ov = by_code.get(code)
        r1y = rm.get("recover_1y") or {}
        rall = rm.get("recover_all") or {}
        # 底层安全性文案
        holdings_summary, holdings_note = "—", "暂无持仓数据"
        if hp:
            parts = [f"{c} {v:.0f}%" for c, v in hp.get("categories", [])]
            holdings_summary = " · ".join(parts) if parts else "—"
            flags = []
            if hp.get("no_stock") is True:
                flags.append("不含股票")
            elif hp.get("no_stock") is False:
                flags.append("含股票")
            if hp.get("has_convertible"):
                flags.append("含可转债")
            else:
                flags.append("不含可转债")
            holdings_note = (
                f"披露债券持仓 · {hp.get('report_period', '')} · "
                f"占净值合计 {hp.get('total_nav_pct', 0):.1f}% · {' · '.join(flags)}"
            )
        rows.append(
            {
                "fund_code": code,
                "fund_name": str(ov.fund_name) if ov is not None else "",
                "max_drawdown_1y": rm.get("max_dd_1y"),
                "max_drawdown_all": rm.get("max_dd_all"),
                "recover_1y_days": r1y.get("days"),
                "recover_1y_range": f"{str(r1y.get('start'))[:10]} → {str(r1y.get('end'))[:10]}" if r1y.get("start") is not None else None,
                "recover_all_days": rall.get("days"),
                "holdings_summary": holdings_summary,
                "holdings_note": holdings_note,
            }
        )
    df = pd.DataFrame(rows)
    # 混合 None/int 的列会被 pandas 转成 float64（None→NaN），统一还原为 None 供下游判空
    for col in ("max_drawdown_1y", "max_drawdown_all", "recover_1y_days", "recover_all_days", "recover_1y_range"):
        if col in df.columns:
            df[col] = df[col].where(pd.notna(df[col]), None)
    return df


def get_bond_risk_comparison(codes: list[str]) -> pd.DataFrame:
    """债基对比：近1年/全历史最大回撤 + 最长回撤修复天数 + 底层安全性（类别占比）。"""
    url, key = _credentials()
    return _bond_risk_comparison(url, key, tuple(normalize_fund_code(c) for c in codes))


def _compute_period_returns_from(frame: pd.DataFrame, periods: list[str]) -> dict[str, float | None]:
    ordered = frame.sort_values("nav_date").reset_index(drop=True)
    result: dict[str, float | None] = {}
    if ordered.empty:
        return {p: None for p in periods}
    # 分红基金（如 008163 每月分红）必须用复权净值，否则区间收益被低估；无复权时回退单位净值
    col = "adjusted_nav" if "adjusted_nav" in ordered.columns and ordered["adjusted_nav"].notna().any() else "unit_nav"
    # 关键加固：净值列为空的行不能参与计算 —— 否则基准日恰好落在空值上时，
    # base 会是 NaN，区间收益一路变成 NaN，界面显示成 "nan%"（新增基金曾出现）。
    ordered = ordered.dropna(subset=[col]).reset_index(drop=True)
    if ordered.empty:
        return {p: None for p in periods}
    latest_ts = ordered["nav_date"].iloc[-1]
    latest = float(ordered[col].iloc[-1])
    if pd.isna(latest) or not latest:
        return {p: None for p in periods}
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
                result[label] = (latest / base - 1.0) * 100.0 if base and not pd.isna(base) else None
                continue
            cutoff = latest_ts - pd.Timedelta(days=days)
        past = ordered[ordered["nav_date"] <= cutoff]
        base = float(past[col].iloc[-1]) if not past.empty else float(ordered[col].iloc[0])
        result[label] = (latest / base - 1.0) * 100.0 if base and not pd.isna(base) else None
    return result


def _meta_from_catalog_and_profiles(
    code: str,
    profiles: pd.DataFrame,
    catalog: dict[str, dict] | None = None,
) -> dict:
    """目录（类别/面板）+ 基金档案（名称/类型/基准）合成展示元数据。"""
    catalog = get_fund_catalog() if catalog is None else catalog
    meta = dict(
        catalog.get(
            code,
            {
                "fund_code": code,
                "fund_name": "",
                "category": "",
                "panel": "净值",
                "fund_type": "",
                "tracking_index": "",
            },
        )
    )
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


# ---------- 国债期货加仓信号（panel=债基，如 007171） ----------
BOND_RANGE_OPTIONS = ["近1月", "近3月", "近6月", "近1年", "近3年", "全部"]
_BOND_RANGE_DAYS = {"近1月": 30, "近3月": 90, "近6月": 180, "近1年": 365, "近3年": 365 * 3}


def get_bond_signal_codes() -> list[str]:
    """配置中需要显示「国债期货加仓信号」的基金（panel=债基）。"""
    codes: list[str] = []
    for category in get_fund_categories().values():
        if category.panel not in BOND_SIGNAL_PANELS:
            continue
        for code in category.fund_codes:
            normalized = normalize_fund_code(code)
            if normalized not in codes:
                codes.append(normalized)
    return codes


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _macro_rates(url: str, key: str, rate_code: str) -> pd.DataFrame:
    """落库宏观序列（cn_10y / bond_futures_tf / bond_futures_t），rate_date→trade_date。"""
    frame = fetch_macro_rates(_client_for(url, key), rate_code)
    if not frame.empty and "rate_date" in frame.columns:
        frame = frame.rename(columns={"rate_date": "trade_date"})
    return frame


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _bond_futures_history(url: str, key: str, code: str, start: str | None) -> dict:
    """国债期货加仓信号历史数据：TF/T 日线 + 历史买入点位（全历史算点位再按窗口裁剪）。"""
    from src.fetchers.bond_futures_fetcher import BOND_FUTURES
    from src.indicators.bond_signal import mark_buy_points

    daily: dict[str, pd.DataFrame] = {}
    for _symbol, rate_code, _name in BOND_FUTURES:
        frame = _macro_rates(url, key, rate_code)
        if not frame.empty and "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        daily[rate_code] = frame

    nav = _nav_history(url, key, normalize_fund_code(code), None, None)
    if not nav.empty and "trade_date" in nav.columns:
        nav = nav.rename(columns={"trade_date": "nav_date"})

    # 全历史计算买入点位（保证窗口首日涨跌幅正确），再按 start 裁剪展示
    tf_df = daily.get("bond_futures_tf", pd.DataFrame())
    t_df = daily.get("bond_futures_t", pd.DataFrame())
    points = mark_buy_points(tf_df, t_df, nav) if not tf_df.empty else pd.DataFrame()

    if start:
        start_ts = pd.Timestamp(start)
        tf_df = tf_df[tf_df["trade_date"] >= start_ts] if not tf_df.empty else tf_df
        t_df = t_df[t_df["trade_date"] >= start_ts] if not t_df.empty else t_df
        points = points[points["trade_date"] >= start_ts] if not points.empty else points

    return {"tf": tf_df, "t": t_df, "points": points}


def get_bond_futures_history(code: str, range_key: str = "近1年") -> dict:
    """某债基的国债期货历史信号数据（TF/T 日线 + 买入点位）。"""
    url, key = _credentials()
    start = None if range_key == "全部" else (date.today() - timedelta(days=_BOND_RANGE_DAYS.get(range_key, 365))).isoformat()
    return _bond_futures_history(url, key, normalize_fund_code(code), start)


# 交易状态判定：可操作窗口 = 交易日 9:30 ~ 15:00（15:00 为场外基金申购截止）
_MARKET_OPEN_START = time(9, 30)
_MARKET_OPEN_END = time(15, 0)

# 中国市场统一按 UTC+8 北京时间判定（中国无夏令时，固定偏移即可，避免依赖 tzdata；
# 不依赖机器本地时区，避免部署在非 UTC+8 环境时误判交易时间）
_BJ_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def _market_status(quotes: dict, now: datetime | None = None) -> dict:
    """根据 TF 分钟线最新数据时间与当前时刻判定交易状态。

    时区：一律按 UTC+8 北京时间——now 取北京时间，分钟数据（naive 北京时间）
    按 UTC+8 解释，带时区则转到北京时间。不依赖机器本地时区。

    :return: {data_time, is_open, close_in_seconds, status_text}
    - is_open=True：分钟数据为今天且当前处于 9:30~15:00 可操作窗口（close_in_seconds=距 15:00 秒数）
    - 否则返回 status_text 说明原因（非交易日/未开盘/已收盘/数据缺失），绝不模拟
    """
    now = now or datetime.now(_BJ_TZ)
    tf = quotes.get("tf", {})
    data_time = tf.get("data_time")
    result = {
        "data_time": data_time,
        "is_open": False,
        "close_in_seconds": None,
        "status_text": "暂无盘中数据，无法判断交易状态",
    }
    if not data_time:
        return result
    try:
        data_dt = datetime.fromisoformat(data_time)
    except ValueError:
        result["status_text"] = "分钟数据时间无法解析，无法判断交易状态"
        return result
    # 分钟数据为北京时间（naive）→ 按 UTC+8 解释；带时区则统一转到北京时间
    if data_dt.tzinfo is None:
        data_dt = data_dt.replace(tzinfo=_BJ_TZ)
    else:
        data_dt = data_dt.astimezone(_BJ_TZ)
    if data_dt.date() != now.date():
        result["status_text"] = f"分钟数据为最近交易日 {data_dt:%m-%d}（当前非交易时间）"
        return result
    if now.time() < _MARKET_OPEN_START:
        result["status_text"] = "当前未开盘（9:30 后可查看今日盘中信号）"
        return result
    if now.time() > _MARKET_OPEN_END:
        result["status_text"] = "今日已收盘（基金申购 15:00 截止）"
        return result
    close_dt = datetime.combine(now.date(), _MARKET_OPEN_END, tzinfo=_BJ_TZ)
    seconds = int((close_dt - now).total_seconds())
    result.update(is_open=True, close_in_seconds=max(seconds, 0), status_text="交易时间")
    return result


def get_bond_futures_signal(code: str) -> dict:
    """国债期货加仓信号（今日盘中判断，实时拉取不缓存）。

    TF/T 当日分钟线（akshare 实时）对比落库日线前收 → 今日涨跌幅；
    前 2 日债基净值日涨跌取落库净值最近 2 个交易日（daily_return 为 %）；
    market 给出交易状态（是否可操作窗口 + 距 15:00 秒数 + 数据时间）。
    数据纪律：拿不到返回 None，绝不模拟。
    """
    from src.fetchers.bond_futures_fetcher import BOND_FUTURES, fetch_bond_futures_intraday
    from src.indicators.bond_signal import evaluate

    code = normalize_fund_code(code)
    url, key = _credentials()

    quotes: dict[str, dict] = {}
    intraday: dict[str, pd.DataFrame] = {}
    for symbol, rate_code, _name in BOND_FUTURES:
        key_short = rate_code.replace("bond_futures_", "")  # tf / t
        quotes[key_short] = {"price": None, "pct": None, "session_date": None, "data_time": None, "prev_close": None}
        try:
            daily = _macro_rates(url, key, rate_code)
            minute = fetch_bond_futures_intraday(symbol)
            if daily is None or daily.empty or minute is None or minute.empty:
                continue
            daily = daily.copy()
            daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce")
            minute["datetime"] = pd.to_datetime(minute["datetime"], errors="coerce")
            minute = minute.dropna(subset=["datetime", "close"]).sort_values("datetime")
            if minute.empty:
                continue
            session_date = minute["datetime"].dt.date.max()
            last_dt = minute["datetime"].iloc[-1]
            prev = daily[daily["trade_date"].dt.date < session_date]
            prev_close = float(prev["rate_value"].iloc[-1]) if not prev.empty else None
            last_price = float(minute["close"].iloc[-1])
            quotes[key_short] = {
                "price": last_price,
                "pct": (last_price / prev_close - 1) * 100.0 if prev_close else None,
                "session_date": session_date.isoformat(),
                "data_time": last_dt.isoformat(),
                "prev_close": prev_close,
            }
            # 当日分钟数据（供 K 线可视化；接口返回多个交易日，只保留最新交易日 = 当日）
            session_df = minute[minute["datetime"].dt.date == session_date]
            intraday[key_short] = session_df[["datetime", "open", "high", "low", "close", "volume"]].copy()
            # 当日 OHLC 汇总（供指标卡片展示）
            ohlc = session_df.dropna(subset=["open", "high", "low", "close"])
            if not ohlc.empty:
                quotes[key_short]["day"] = {
                    "open": float(ohlc["open"].iloc[0]),
                    "high": float(ohlc["high"].max()),
                    "low": float(ohlc["low"].min()),
                    "close": float(ohlc["close"].iloc[-1]),
                }
        except Exception:  # noqa: BLE001
            continue

    # 前 2 日债基净值日涨跌（%）（近 90 天窗口足够覆盖 2 个交易日）
    nav_prev2: list[float] = []
    try:
        nav = _nav_history(url, key, code, (date.today() - timedelta(days=90)).isoformat(), None)
        if not nav.empty and "daily_return" in nav.columns:
            nav_prev2 = nav["daily_return"].astype(float).dropna().tolist()[-2:]
    except Exception:  # noqa: BLE001
        nav_prev2 = []

    signal = evaluate(quotes.get("tf", {}).get("pct"), quotes.get("t", {}).get("pct"), nav_prev2)
    return {
        "tf": quotes.get("tf", {}),
        "t": quotes.get("t", {}),
        "nav_prev2": nav_prev2,
        "signal": signal,
        "market": _market_status(quotes),
        "intraday": intraday,
    }


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _nav_history_batch(url: str, key: str, codes: tuple[str, ...], start: str | None, end: str | None) -> pd.DataFrame:
    """批量拉取多只基金净值：.in(fund_code) 一次查询（总览合并用，替代逐只 8 次往返）。"""
    if not codes:
        return pd.DataFrame(columns=["fund_code", "nav_date", "unit_nav", "adjusted_nav", "daily_return"])
    client = _client_for(url, key)
    qb = (
        client.table("fund_nav_history")
        .select("fund_code, trade_date, unit_nav, adjusted_nav, daily_return")
        .in_("fund_code", list(codes))
        .order("trade_date")
    )
    if start:
        qb = qb.gte("trade_date", start)
    if end:
        qb = qb.lte("trade_date", end)
    data = _fetch_all_rows(qb)
    if not data:
        return pd.DataFrame(columns=["fund_code", "nav_date", "unit_nav", "adjusted_nav", "daily_return"])
    df = pd.DataFrame(data)
    df["fund_code"] = df["fund_code"].astype(str).apply(normalize_fund_code)
    df["nav_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["unit_nav"] = pd.to_numeric(df["unit_nav"], errors="coerce")
    if "adjusted_nav" in df.columns:
        df["adjusted_nav"] = pd.to_numeric(df["adjusted_nav"], errors="coerce")
    if "daily_return" in df.columns:
        df["daily_return"] = pd.to_numeric(df["daily_return"], errors="coerce")
    df = df.drop(columns=["trade_date"]).dropna(subset=["nav_date", "unit_nav"]).reset_index(drop=True)
    return df


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _all_funds_overview(url: str, key: str, codes: tuple[str, ...]) -> pd.DataFrame:
    profiles = _fund_profiles(url, key)
    catalog = get_fund_catalog()
    # 首页只用 近1周/近1月/近3月（自然月回推最多 3 个月）→ 150 天足够且有缓冲。
    # 原来取 400 天，会让每只基金多拉 2/3 的净值行，批量分页请求数也随之翻倍。
    start = (date.today() - timedelta(days=150)).isoformat()
    navs = _nav_history_batch(url, key, tuple(codes), start, None)
    rows = []
    for code in codes:
        code = normalize_fund_code(code)
        meta = _meta_from_catalog_and_profiles(code, profiles, catalog)
        frame = (
            navs[navs["fund_code"] == code].sort_values("nav_date").reset_index(drop=True)
            if not navs.empty
            else pd.DataFrame()
        )
        latest = _compute_latest_from(frame)
        period = _compute_period_returns_from(frame, ["近1周", "近1月", "近3月"])
        rows.append(
            {
                "fund_code": code,
                "fund_name": meta["fund_name"] or code,
                "category": meta["category"],
                "panel": meta["panel"],
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
    # 单位净值为空/NaN 的行不能作为"最新净值"（否则卡片会显示 nan）
    ordered = frame.sort_values("nav_date").dropna(subset=["unit_nav"]).reset_index(drop=True)
    if ordered.empty:
        return {"nav_date": None, "unit_nav": None, "daily_return_pct": None}
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
    catalog = get_fund_catalog()
    codes = tuple(catalog.keys())
    overview = _all_funds_overview(url, key, codes)
    latest_date = None
    if not overview.empty:
        dates = pd.to_datetime(overview["nav_date"], errors="coerce").dropna()
        if not dates.empty:
            latest_date = dates.max().strftime("%Y-%m-%d")
    strategy_count = sum(1 for code in codes if catalog.get(code, {}).get("panel") == "红利低波")
    jobs = _sync_jobs(url, key)
    last_sync = jobs["executed_at"].max().strftime("%Y-%m-%d %H:%M:%S") if not jobs.empty else "暂无"
    return {
        "fund_count": len(codes),
        "latest_nav_date": latest_date or "—",
        "strategy_fund_count": strategy_count,
        "last_sync": last_sync,
    }


# 指数名称兜底（库表 index_master 无名称时用；再没有就用代码本身）
_FALLBACK_INDEX_NAMES = {
    "000001": "上证指数",
    "000300": "沪深300",
    "399001": "深证成指",
    "399006": "创业板指",
}


def get_market_indexes() -> list[dict]:
    """市场指数条（真实数据：index_daily_history 最新收盘/涨跌幅）。"""
    if not is_connected():
        return []
    url, key = _credentials()
    return _market_indexes(url, key)


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _index_list(url: str, key: str, list_key: str) -> dict:
    """界面指数列表（库表唯一来源）；配置表未建时 available=False。"""
    return fetch_index_list(_client_for(url, key), list_key)


def get_market_index_codes() -> list[str]:
    """市场指数条展示的指数代码（服务器配置，顺序即展示顺序）。

    配置表未建 / 数据库不可达 / 未连接 → 空列表（不回退文件配置）。
    """
    if not is_connected():
        return []
    try:
        url, key = _credentials()
        return _index_list(url, key, LIST_MARKET_INDEXES)["codes"]
    except Exception:  # noqa: BLE001 - 网络/权限异常 → 视为未配置
        return []


# ---------- 市场指数条配置（网页端增删，与基金配置同一套路） ----------
def get_market_index_snapshot() -> dict:
    """配置页用：指数条配置 + 每个指数当前是否有行情。

    :return: ``{"available": bool, "items": [{"code","name","value"}]}``；
             ``available=False`` 表示配置表尚未创建。
    """
    url, key = _credentials()
    data = _index_list(url, key, LIST_MARKET_INDEXES)
    if not data["available"]:
        return {"available": False, "items": []}
    quotes = {item["code"]: item for item in _market_indexes(url, key)}
    items = [
        {
            "code": code,
            "name": quotes.get(code, {}).get("name") or code,
            "value": quotes.get(code, {}).get("value"),
        }
        for code in data["codes"]
    ]
    return {"available": True, "items": items}


def add_market_index(index_code: str) -> None:
    """把指数加入市场指数条（追加到末尾）。"""
    url, key = _credentials()
    add_index(_client_for(url, key), LIST_MARKET_INDEXES, index_code)
    _invalidate_caches()


def remove_market_index(index_code: str) -> None:
    """把指数移出市场指数条。"""
    url, key = _credentials()
    remove_index(_client_for(url, key), LIST_MARKET_INDEXES, index_code)
    _invalidate_caches()


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _market_indexes(url: str, key: str) -> list[dict]:
    """市场指数条：服务器配置的指数（ui_index_list）最新行情。

    实现要点：
    - **一次批量查询**取回所有展示指数最近若干行，再在内存里取每个指数最新一条
      （原来每个指数一次查询，4 个指数就是 4 次往返）；
    - 该查询走 _execute_with_retry，网络抖动会自动重试（此前这条路径没有重试）；
    - 名称优先取库表 index_master.index_name（同步任务按 [indexes.registry] 写入），
      其次内置兜底名，最后回落为代码本身；
    - 拿不到行情时 value=None（界面显示「暂无」），绝不模拟数值。
    """
    client = _client_for(url, key)
    codes = get_market_index_codes()
    if not codes:
        return []

    names: dict[str, str] = {}
    try:
        for row in _fetch_all_rows(client.table("index_master").select("index_code,index_name")):
            code = str(row.get("index_code") or "").strip().upper()
            name = str(row.get("index_name") or "").strip()
            if code and name:
                names[code] = name
    except Exception:  # noqa: BLE001 - 名称取不到不影响行情展示
        names = {}

    response = _execute_with_retry(
        client.table("index_daily_history")
        .select("index_code,trade_date,close,change_pct")
        .in_("index_code", list(codes))
        .order("trade_date", desc=True)
        .limit(max(20 * len(codes), 40))
    )
    latest: dict[str, dict] = {}
    for row in response.data or []:
        code = str(row.get("index_code") or "").strip().upper()
        if code not in latest:
            latest[code] = row

    result: list[dict] = []
    for code in codes:
        name = names.get(code) or _FALLBACK_INDEX_NAMES.get(code, code)
        row = latest.get(code)
        if row:
            result.append(
                {
                    "code": code,
                    "name": name,
                    "value": float(row["close"]),
                    "change_pct": float(row.get("change_pct") or 0.0),
                }
            )
        else:
            result.append({"code": code, "name": name, "value": None, "change_pct": None})
    return result


@lru_cache(maxsize=16)
@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _benchmark_frame(url: str, key: str, index_code: str = "000300S") -> pd.DataFrame:
    """指定大盘指数（默认沪深300全收益 000300S）真实走势，用于归一化对比。"""
    client = _client_for(url, key)
    rows = _fetch_all_rows(
        client.table("index_daily_history").select("trade_date,close").eq("index_code", index_code)
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


def get_index_benchmark(index_code: str, range_key: str = "近1年") -> pd.DataFrame:
    """指定大盘指数的真实走势（供业绩走势对比下拉框），按范围裁剪。"""
    url, key = _credentials()
    frame = _benchmark_frame(url, key, str(index_code).strip())
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


# ---------- RSI 动能看板（派生计算，不落库） ----------
# 看板时间范围（周 RSI 需要较长历史，默认近 3 年，符合用户「近 3~5 年」建议）
RSI_RANGE_OPTIONS = ["近1年", "近3年", "近5年", "全部"]
RSI_RANGE_DAYS = {"近1年": 365, "近3年": 365 * 3, "近5年": 365 * 5}


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _rsi_dashboard_full(url: str, key: str, code: str) -> dict:
    """某基金的 RSI 看板数据（缓存）：基于【全历史】净值计算全部指标。

    净值走 _nav_history（复用净值缓存）；spread 走 _strategy_factors（复用指数层因子缓存，
    无映射基金如 008163 返回空 → 看板无利差线，其余照常计算，绝不模拟）。

    **关键**：必须全历史计算后再由 slice_rsi_dashboard 按显示窗口裁剪，否则 250 日均线
    在窗口起始处缺少一年预热数据，会形成被高起点拉偏的伪拟合线（用户反馈 Bug）。
    """
    nav = _nav_history(url, key, code, None, None)
    if nav.empty:
        return {}
    factors = _strategy_factors(url, key, code)
    from src.indicators.rsi import build_rsi_dashboard

    return build_rsi_dashboard(nav, factors)


def get_rsi_dashboard(code: str, range_key: str = "近3年") -> dict:
    """某只基金 RSI 动能看板（日/周 RSI、250MA、信号、前瞻统计、股息率利差）。

    基于全历史计算，再按 range_key 裁剪显示窗口（250MA/RSI/信号在窗口起始处即正确）。
    数据纪律：全部由真实净值/指数因子派生；spread 只在该基金有策略指数映射时才有，
    无映射基金（如 008163）对应字段为空，界面显示「暂无」，绝不模拟。
    """
    url, key = _credentials()
    data = _rsi_dashboard_full(url, key, normalize_fund_code(code))
    if not data:
        return data
    days = RSI_RANGE_DAYS.get(range_key)
    if days is None:
        return data
    from src.indicators.rsi import slice_rsi_dashboard

    start = date.today() - timedelta(days=days)
    return slice_rsi_dashboard(data, pd.Timestamp(start))


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
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


@st.cache_data(ttl=_CACHE_TTL)
def _server_counts(url: str, key: str) -> tuple[int, int]:
    """存储服务器（Supabase）数据行数与表数（**估算值**）。

    用户允许近似：count="planned" 用 PostgreSQL 规划器估算，秒级返回，不保证精确，
    页面标注「估算」。11 张表并行（网络往返并发），每线程独立 client 避免共享连接并发问题。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    main_tables = (
        "fund_nav_history",
        "fund_dividends",
        "index_daily_history",
        "index_daily_factors",
        "index_valuation_history",
        "macro_rates_history",
        "fund_profiles",
        "fund_tracking_index",
        "fund_snapshot_metrics",
        "index_master",
        "sync_job",
        "sync_watermark",
    )

    def _count_table(table: str) -> int:
        try:
            client = create_supabase_client(SupabaseSettings(url=url, key=key))
            resp = client.table(table).select("*", count="planned").limit(1).execute()
            return int(resp.count or 0)
        except Exception:  # noqa: BLE001 - 单表失败不阻塞整体
            return 0

    db_rows = 0
    tables = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_count_table, t) for t in main_tables]
        for future in as_completed(futures):
            count = future.result()
            db_rows += count
            tables += 1 if count > 0 else 0
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
        _fund_config,
        _index_list,
        _nav_history,
        _nav_with_cumulative,
        _fund_profiles,
        _dividends,
        _strategy_factors,
        _sync_jobs,
        _watermarks,
        _all_funds_overview,
        _market_indexes,
        _funds_bond_comparison,
        _bond_risk_comparison,
    ):
        try:
            func.clear()
        except Exception:  # noqa: BLE001
            pass


def clear_read_caches() -> None:
    """清空读取缓存：下次渲染重新读服务器。

    用途：数据在别处（另一台机器 / 命令行同步）更新过时，强制本会话重新拉取。
    对应界面按钮在「🗄️ 数据管理」页。
    """
    _invalidate_caches()


def run_refresh(full: bool = False, progress_callback=None) -> tuple[list[dict], str | None]:
    """执行一次真实刷新。

    :param full: True 时先清空水位，强制全量重拉原始数据并重算因子。
    :param progress_callback: 可选进度回调（done, total, label），用于界面进度条。
    :return: (结果列表, 错误信息)；错误信息为 None 表示整体成功（可能含单项 error）。
    """
    try:
        url, key = _credentials()
        client = _client_for(url, key)
        if full:
            _clear_watermarks(client)
        # 惰性导入：刷新编排较重（含 akshare 抓取链），避免在页面启动时加载
        from src.storage.strategy_sync_runner import refresh_all

        results = refresh_all(client, progress=progress_callback)
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


def run_layer_refresh(layer_key: str, progress_callback=None) -> tuple[list[dict], str | None]:
    """按层刷新（真实同步编排：fund/index/rate/factors 走 strategy_sync_runner.refresh_layer）。

    - fund / index / rate / factors → refresh_layer（拉取 + 水位）
    :param progress_callback: 可选进度回调（done, total, label），用于界面进度条。
    """
    layer = next((x for x in REFRESH_LAYERS if x["key"] == layer_key), None)
    label = f'{layer["icon"]} {layer["label"]}' if layer else layer_key
    try:
        url, key = _credentials()
        client = _client_for(url, key)
        # 惰性导入：刷新编排较重（含 akshare 抓取链），避免在页面启动时加载
        from src.storage.strategy_sync_runner import refresh_layer

        results, error = refresh_layer(client, layer_key, progress=progress_callback)
        _invalidate_caches()
        return results, error or "完成"
    except Exception as exc:  # noqa: BLE001
        return [], f"{label}按层刷新失败：{exc}"
