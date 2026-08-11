from __future__ import annotations

from datetime import datetime
from typing import Iterable
from uuid import uuid4

import pandas as pd
from supabase import Client, create_client

from src.config import IndexSpec, SupabaseSettings, supabase_settings_ready


def normalize_fund_code(code) -> str:
    """基金代码规范化（内联，避免在模块导入期拉取抓取层/akshare）。"""
    normalized = str(code).strip()
    return normalized.zfill(6) if normalized.isdigit() and len(normalized) < 6 else normalized


def _fetch_all_rows(query_builder, *, page_size: int = 1000) -> list[dict]:
    """Fetch all rows from a Supabase query using range pagination."""
    all_rows: list[dict] = []
    start = 0

    while True:
        end = start + page_size - 1
        response = query_builder.range(start, end).execute()
        page_rows = response.data or []
        if not page_rows:
            break

        all_rows.extend(page_rows)
        if len(page_rows) < page_size:
            break

        start += page_size

    return all_rows


def create_supabase_client(settings: SupabaseSettings) -> Client:
    if not supabase_settings_ready(settings):
        raise ValueError("Supabase url/key are not configured")

    return create_client(settings.url, settings.key)


def upsert_fund_profiles(client: Client, fund_profiles: Iterable[dict]) -> None:
    """Upsert 基金档案到 fund_profiles（新 ER 结构：benchmark / is_etf）。

    profile 可含：fund_code / fund_name / fund_type / is_etf / benchmark（或旧键 tracking_index 兼容）。
    """
    rows = []
    for profile in fund_profiles:
        row: dict = {"fund_code": normalize_fund_code(profile.get("fund_code", ""))}
        if profile.get("fund_name"):
            row["fund_name"] = str(profile["fund_name"]).strip()
        if profile.get("fund_type"):
            row["fund_type"] = str(profile["fund_type"]).strip()
        benchmark = profile.get("benchmark") or profile.get("tracking_index")
        if benchmark:
            row["benchmark"] = str(benchmark).strip()
        if profile.get("is_etf") is not None:
            row["is_etf"] = bool(profile["is_etf"])
        if row["fund_code"]:
            rows.append(row)

    if rows:
        client.table("fund_profiles").upsert(rows, on_conflict="fund_code").execute()


def upsert_index_master(client: Client, index_specs: Iterable[IndexSpec]) -> int:
    """Upsert 指数注册表条目到 index_master（配置来自 TOML [indexes.registry]）。"""
    records = []
    for spec in index_specs:
        records.append(
            {
                "index_code": str(spec.index_code).strip(),
                "index_name": str(spec.index_name).strip() or None,
                "index_category": str(spec.index_category).strip() or "strategy",
                "is_total_return": bool(spec.is_total_return),
                "exchange": str(spec.exchange).strip() or None,
                "source": str(spec.source).strip() or "csindex",
            }
        )
    if records:
        client.table("index_master").upsert(records, on_conflict="index_code").execute()
    return len(records)


def upsert_fund_tracking_index(client: Client, tracking_rows: Iterable[tuple[str, str, str]]) -> int:
    """Upsert 基金→指数映射（配置来自 TOML [funds.categories.*].index_codes）。"""
    records = []
    for fund_code, index_code, role in tracking_rows:
        records.append(
            {
                "fund_code": normalize_fund_code(fund_code),
                "index_code": str(index_code).strip(),
                "role": str(role).strip() or "strategy",
            }
        )
    if records:
        client.table("fund_tracking_index").upsert(records, on_conflict="fund_code,index_code").execute()
    return len(records)


def delete_stale_fund_tracking_index(client: Client, keep: Iterable[tuple[str, str]]) -> int:
    """删除 fund_tracking_index 中不在 keep 集合里的 (fund_code, index_code) 行（配置对账）。"""
    keep_set = {(normalize_fund_code(f), str(ic).strip()) for f, ic in keep}
    existing = _fetch_all_rows(client.table("fund_tracking_index").select("fund_code,index_code"))
    stale = [row for row in existing if (str(row.get("fund_code")), str(row.get("index_code"))) not in keep_set]
    for row in stale:
        client.table("fund_tracking_index").delete().eq("fund_code", row["fund_code"]).eq("index_code", row["index_code"]).execute()
    return len(stale)


def delete_stale_index_master(client: Client, keep_codes: Iterable[str], *, referenced: Iterable[str] = ()) -> int:
    """删除 index_master 中不在配置（且未被任何映射引用）的指数行（配置对账）。"""
    keep_set = {str(c).strip() for c in keep_codes} | {str(c).strip() for c in referenced}
    existing = _fetch_all_rows(client.table("index_master").select("index_code"))
    stale = [row for row in existing if str(row.get("index_code")) not in keep_set]
    for row in stale:
        client.table("index_master").delete().eq("index_code", row["index_code"]).execute()
    return len(stale)


def upsert_nav_history(client: Client, nav_df: pd.DataFrame) -> None:
    """Upsert 基金净值到 fund_nav_history（新结构：trade_date / adjusted_nav）。

    nav_df 需含 fund_code / unit_nav，日期列兼容 nav_date 或 trade_date；
    可含 daily_return(%, 建议) 与 adjusted_nav（复权净值，建议入库前由 derive_adjusted_nav 推导）。
    """
    frame = nav_df.copy()
    date_col = "trade_date" if "trade_date" in frame.columns else "nav_date"
    missing_columns = {"fund_code", date_col, "unit_nav"} - set(frame.columns)
    if missing_columns:
        raise ValueError(f"Missing required NAV columns: {sorted(missing_columns)}")

    records = []
    for row in frame.itertuples(index=False):
        record = {
            "fund_code": normalize_fund_code(getattr(row, "fund_code")),
            "trade_date": pd.Timestamp(getattr(row, date_col)).date().isoformat(),
            "unit_nav": float(getattr(row, "unit_nav")),
        }
        if hasattr(row, "daily_return"):
            value = getattr(row, "daily_return")
            if pd.notna(value):
                record["daily_return"] = float(value)
        if hasattr(row, "adjusted_nav"):
            value = getattr(row, "adjusted_nav")
            if pd.notna(value):
                record["adjusted_nav"] = float(value)
        records.append(record)

    if records:
        client.table("fund_nav_history").upsert(records, on_conflict="fund_code,trade_date").execute()


def insert_sync_log(client: Client, *, job_name: str, status: str, message: str, row_count: int) -> None:
    client.table("sync_job").insert(
        [
            {
                "log_id": str(uuid4()),
                "job_name": job_name,
                "status": status,
                "message": message,
                "row_count": row_count,
                "executed_at": datetime.utcnow().isoformat(),
            }
        ]
    ).execute()


def fetch_nav_history(client: Client, fund_code: str, *, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    """Fetch NAV history for one fund, optionally filtered by date range.

    :param start_date/end_date: ISO date strings (YYYY-MM-DD). When given, only
        rows within the range are returned (used by the dashboard to avoid pulling
        the full history on every view).
    """
    query_builder = (
        client.table("fund_nav_history")
        .select("fund_code, trade_date, unit_nav, adjusted_nav, daily_return")
        .eq("fund_code", normalize_fund_code(fund_code))
        .order("trade_date")
    )
    if start_date:
        query_builder = query_builder.gte("trade_date", start_date)
    if end_date:
        query_builder = query_builder.lte("trade_date", end_date)

    data = _fetch_all_rows(query_builder)
    if not data:
        return pd.DataFrame(columns=["fund_code", "nav_date", "unit_nav", "adjusted_nav", "daily_return"])

    df = pd.DataFrame(data)
    df["fund_code"] = df["fund_code"].astype(str).apply(normalize_fund_code)
    df["nav_date"] = pd.to_datetime(df["trade_date"], errors="coerce")  # API 兼容层：trade_date → nav_date
    df["unit_nav"] = pd.to_numeric(df["unit_nav"], errors="coerce")
    if "adjusted_nav" in df.columns:
        df["adjusted_nav"] = pd.to_numeric(df["adjusted_nav"], errors="coerce")
    if "daily_return" in df.columns:
        df["daily_return"] = pd.to_numeric(df["daily_return"], errors="coerce")
    df = df.drop(columns=["trade_date"])
    return df.dropna(subset=["nav_date", "unit_nav"]).reset_index(drop=True)


def upsert_fund_dividends(client: Client, dividend_df: pd.DataFrame) -> int:
    """Upsert fund dividend records. Returns the number of records written."""
    required_columns = {"fund_code", "ex_date", "dividend_per_unit"}
    missing_columns = required_columns - set(dividend_df.columns)
    if missing_columns:
        raise ValueError(f"Missing required dividend columns: {sorted(missing_columns)}")

    records = []
    for row in dividend_df.copy().itertuples(index=False):
        records.append(
            {
                "fund_code": normalize_fund_code(getattr(row, "fund_code")),
                "ex_date": pd.Timestamp(getattr(row, "ex_date")).date().isoformat(),
                "dividend_per_unit": float(getattr(row, "dividend_per_unit")),
            }
        )

    if records:
        client.table("fund_dividends").upsert(records, on_conflict="fund_code,ex_date").execute()
    return len(records)


def upsert_macro_rates(client: Client, rate_df: pd.DataFrame, *, source: str = "official") -> int:
    """Upsert 宏观利率到 macro_rates_history（新结构：trade_date / source）。"""
    frame = rate_df.copy()
    date_col = "trade_date" if "trade_date" in frame.columns else "rate_date"
    missing_columns = {"rate_code", date_col, "rate_value"} - set(frame.columns)
    if missing_columns:
        raise ValueError(f"Missing required rate columns: {sorted(missing_columns)}")

    records = []
    for row in frame.itertuples(index=False):
        records.append(
            {
                "rate_code": str(getattr(row, "rate_code")).strip(),
                "trade_date": pd.Timestamp(getattr(row, date_col)).date().isoformat(),
                "rate_value": float(getattr(row, "rate_value")),
                "source": source,
            }
        )

    if records:
        client.table("macro_rates_history").upsert(records, on_conflict="rate_code,trade_date").execute()
    return len(records)


def upsert_index_valuations(
    client: Client, valuation_df: pd.DataFrame, *, source: str = "csindex"
) -> int:
    """Upsert 指数估值到 index_valuation_history（新结构：pe_ttm/pe_lyr/dividend_yield）。

    兼容输入列：新列（pe_ttm/pe_lyr/dividend_yield）或旧列（pe1/pe2/dividend_yield1/2）。
    """
    frame = valuation_df.copy()
    date_col = "trade_date" if "trade_date" in frame.columns else "date"
    missing_columns = {"index_code", date_col} - set(frame.columns)
    if missing_columns:
        raise ValueError(f"Missing required valuation columns: {sorted(missing_columns)}")

    def _val(row, *names):
        for name in names:
            if hasattr(row, name):
                value = getattr(row, name)
                if pd.notna(value):
                    return float(value)
        return None

    records = []
    for row in frame.itertuples(index=False):
        record: dict = {
            "index_code": str(getattr(row, "index_code")).strip(),
            "trade_date": pd.Timestamp(getattr(row, date_col)).date().isoformat(),
            "source": source,
        }
        pe_ttm = _val(row, "pe_ttm", "pe1")
        pe_lyr = _val(row, "pe_lyr", "pe2")
        dividend_yield = _val(row, "dividend_yield", "dividend_yield1")
        if pe_ttm is not None:
            record["pe_ttm"] = pe_ttm
        if pe_lyr is not None:
            record["pe_lyr"] = pe_lyr
        if dividend_yield is not None:
            record["dividend_yield"] = dividend_yield
        records.append(record)

    if records:
        client.table("index_valuation_history").upsert(records, on_conflict="index_code,trade_date").execute()
    return len(records)


def upsert_index_daily_history(client: Client, daily_df: pd.DataFrame) -> int:
    """Upsert 指数日行情到 index_daily_history（新结构：index_code/trade_date/OHLC/...）。"""
    frame = daily_df.copy()
    date_col = "trade_date" if "trade_date" in frame.columns else "date"
    missing_columns = {"index_code", date_col, "close"} - set(frame.columns)
    if missing_columns:
        raise ValueError(f"Missing required index daily columns: {sorted(missing_columns)}")

    records = []
    for row in frame.itertuples(index=False):
        record: dict = {
            "index_code": str(getattr(row, "index_code")).strip(),
            "trade_date": pd.Timestamp(getattr(row, date_col)).date().isoformat(),
            "close": float(getattr(row, "close")),
        }
        for column in ("open", "high", "low", "change_pct", "volume", "amount"):
            if hasattr(row, column) and pd.notna(getattr(row, column)):
                record[column] = float(getattr(row, column))
        for column in ("index_type", "source"):
            if hasattr(row, column) and getattr(row, column) not in (None, ""):
                record[column] = str(getattr(row, column)).strip()
        records.append(record)

    if records:
        client.table("index_daily_history").upsert(records, on_conflict="index_code,trade_date").execute()
    return len(records)


def upsert_index_daily_factors(client: Client, factors_df: pd.DataFrame) -> int:
    """Upsert 指数策略因子到 index_daily_factors（新结构，指数层）。"""
    frame = factors_df.copy()
    date_col = "trade_date" if "trade_date" in frame.columns else "date"
    missing_columns = {"index_code", date_col} - set(frame.columns)
    if missing_columns:
        raise ValueError(f"Missing required factor columns: {sorted(missing_columns)}")

    float_columns = {
        "dividend_yield", "annualized_volatility", "max_drawdown", "dividend_yield_percentile",
        "spread", "spread_percentile", "dy_vol_ratio_percentile", "drawdown_percentile",
        "volatility_percentile", "score_a", "score_b",
    }
    bool_columns = {"signal_a", "signal_b"}

    records = []
    for row in frame.itertuples(index=False):
        record: dict = {
            "index_code": str(getattr(row, "index_code")).strip(),
            "trade_date": pd.Timestamp(getattr(row, date_col)).date().isoformat(),
        }
        for column in float_columns:
            if hasattr(row, column) and pd.notna(getattr(row, column)):
                record[column] = float(getattr(row, column))
        for column in bool_columns:
            if hasattr(row, column) and pd.notna(getattr(row, column)):
                record[column] = bool(getattr(row, column))
        records.append(record)

    if records:
        client.table("index_daily_factors").upsert(records, on_conflict="index_code,trade_date").execute()
    return len(records)


def delete_index_daily_factors(client: Client, index_code: str) -> None:
    """删除某指数的策略因子（重算前清理，避免旧口径残留）。"""
    client.table("index_daily_factors").delete().eq("index_code", str(index_code).strip()).execute()


def upsert_watermark(client: Client, entity_type: str, entity_code: str, last_date, source: str | None = None) -> None:
    """Write/update the sync watermark for one entity."""
    normalized_code = normalize_fund_code(entity_code) if entity_type == "fund" else str(entity_code)
    row = {
        "entity_type": entity_type,
        "entity_code": normalized_code,
        "last_date": pd.Timestamp(last_date).date().isoformat(),
    }
    if source:
        row["source"] = source
    client.table("sync_watermark").upsert([row], on_conflict="entity_type,entity_code").execute()


def list_watermarks(client: Client) -> pd.DataFrame:
    """List all sync watermarks (used by the dashboard to show refresh state)."""
    query_builder = client.table("sync_watermark").select("entity_type, entity_code, last_date, source, updated_at").order("updated_at", desc=True)
    data = _fetch_all_rows(query_builder)
    if not data:
        return pd.DataFrame(columns=["entity_type", "entity_code", "last_date", "source", "updated_at"])

    df = pd.DataFrame(data)
    df["last_date"] = pd.to_datetime(df["last_date"], errors="coerce")
    df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce")
    return df


def fetch_fund_dividends(client: Client, fund_code: str) -> pd.DataFrame:
    """Fetch all dividend records for one fund."""
    query_builder = (
        client.table("fund_dividends")
        .select("fund_code, ex_date, dividend_per_unit")
        .eq("fund_code", normalize_fund_code(fund_code))
        .order("ex_date")
    )
    data = _fetch_all_rows(query_builder)
    if not data:
        return pd.DataFrame(columns=["fund_code", "ex_date", "dividend_per_unit"])

    df = pd.DataFrame(data)
    df["fund_code"] = df["fund_code"].astype(str).apply(normalize_fund_code)
    df["ex_date"] = pd.to_datetime(df["ex_date"], errors="coerce")
    df["dividend_per_unit"] = pd.to_numeric(df["dividend_per_unit"], errors="coerce")
    return df.dropna(subset=["ex_date", "dividend_per_unit"]).reset_index(drop=True)


def fetch_macro_rates(client: Client, rate_code: str = "cn_10y") -> pd.DataFrame:
    """Fetch all rate history for one rate code.

    新 schema 时间列统一为 trade_date；对外兼容返回 rate_date（API 兼容层）。
    """
    query_builder = (
        client.table("macro_rates_history")
        .select("rate_code, trade_date, rate_value")
        .eq("rate_code", rate_code)
        .order("trade_date")
    )
    data = _fetch_all_rows(query_builder)
    if not data:
        return pd.DataFrame(columns=["rate_code", "rate_date", "rate_value"])

    df = pd.DataFrame(data)
    df["rate_code"] = df["rate_code"].astype(str)
    df["rate_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.drop(columns=["trade_date"])
    df["rate_value"] = pd.to_numeric(df["rate_value"], errors="coerce")
    return df.dropna(subset=["rate_date", "rate_value"]).reset_index(drop=True)


def list_fund_profiles(client: Client) -> pd.DataFrame:
    query_builder = (
        client.table("fund_profiles")
        .select("fund_code, fund_name, fund_type, is_etf, benchmark, created_at")
        .order("fund_code")
    )
    data = _fetch_all_rows(query_builder)
    if not data:
        return pd.DataFrame(columns=["fund_code"])

    df = pd.DataFrame(data)
    df["fund_code"] = df["fund_code"].astype(str).apply(normalize_fund_code)
    if "fund_name" in df.columns:
        df["fund_name"] = df["fund_name"].astype(object).where(df["fund_name"].notna(), None)
    return df


