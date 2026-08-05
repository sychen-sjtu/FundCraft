from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable
from uuid import uuid4

import pandas as pd
from supabase import Client, create_client

from src.config import SupabaseSettings, load_supabase_settings, supabase_settings_ready
from src.fetchers.akshare_fund_nav import normalize_fund_code


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


def get_supabase_client(project_root: Path | None = None) -> tuple[Client, SupabaseSettings]:
    settings = load_supabase_settings(project_root)
    client = create_supabase_client(settings)
    return client, settings


def upsert_fund_profiles(client: Client, fund_profiles: Iterable[dict]) -> None:
    """Upsert fund profiles. Each dict may contain fund_code, fund_name, fund_type, tracking_index."""
    rows = []
    for profile in fund_profiles:
        row: dict = {"fund_code": normalize_fund_code(profile.get("fund_code", ""))}
        if profile.get("fund_name"):
            row["fund_name"] = str(profile["fund_name"]).strip()
        if profile.get("fund_type"):
            row["fund_type"] = str(profile["fund_type"]).strip()
        if profile.get("tracking_index"):
            row["tracking_index"] = str(profile["tracking_index"]).strip()
        if row["fund_code"]:
            rows.append(row)

    if rows:
        client.table("fund_profiles").upsert(rows, on_conflict="fund_code").execute()


def upsert_nav_history(client: Client, nav_df: pd.DataFrame) -> None:
    required_columns = {"fund_code", "nav_date", "unit_nav"}
    missing_columns = required_columns - set(nav_df.columns)
    if missing_columns:
        raise ValueError(f"Missing required NAV columns: {sorted(missing_columns)}")

    records = []
    for row in nav_df.copy().itertuples(index=False):
        record = {
            "fund_code": normalize_fund_code(getattr(row, "fund_code")),
            "nav_date": pd.Timestamp(getattr(row, "nav_date")).date().isoformat(),
            "unit_nav": float(getattr(row, "unit_nav")),
        }
        if hasattr(row, "daily_return"):
            daily_return_value = getattr(row, "daily_return")
            if pd.notna(daily_return_value):
                record["daily_return"] = float(daily_return_value)
        records.append(record)

    if records:
        client.table("fund_nav_history").upsert(records, on_conflict="fund_code,nav_date").execute()


def insert_sync_log(client: Client, *, job_name: str, status: str, message: str, row_count: int) -> None:
    client.table("sync_jobs").insert(
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
        .select("fund_code, nav_date, unit_nav, daily_return")
        .eq("fund_code", normalize_fund_code(fund_code))
        .order("nav_date")
    )
    if start_date:
        query_builder = query_builder.gte("nav_date", start_date)
    if end_date:
        query_builder = query_builder.lte("nav_date", end_date)

    data = _fetch_all_rows(query_builder)
    if not data:
        return pd.DataFrame(columns=["fund_code", "nav_date", "unit_nav", "daily_return"])

    df = pd.DataFrame(data)
    df["fund_code"] = df["fund_code"].astype(str).apply(normalize_fund_code)
    df["nav_date"] = pd.to_datetime(df["nav_date"], errors="coerce")
    df["unit_nav"] = pd.to_numeric(df["unit_nav"], errors="coerce")
    if "daily_return" in df.columns:
        df["daily_return"] = pd.to_numeric(df["daily_return"], errors="coerce")
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


def upsert_macro_rates(client: Client, rate_df: pd.DataFrame) -> int:
    """Upsert macro rate history. Returns the number of records written."""
    required_columns = {"rate_code", "rate_date", "rate_value"}
    missing_columns = required_columns - set(rate_df.columns)
    if missing_columns:
        raise ValueError(f"Missing required rate columns: {sorted(missing_columns)}")

    records = []
    for row in rate_df.copy().itertuples(index=False):
        records.append(
            {
                "rate_code": str(getattr(row, "rate_code")).strip(),
                "rate_date": pd.Timestamp(getattr(row, "rate_date")).date().isoformat(),
                "rate_value": float(getattr(row, "rate_value")),
            }
        )

    if records:
        client.table("macro_rates_history").upsert(records, on_conflict="rate_code,rate_date").execute()
    return len(records)


def upsert_index_valuations(client: Client, valuation_df: pd.DataFrame) -> int:
    """Upsert index valuation rows into index_valuation_history. Returns row count."""
    required_columns = {"index_code", "trade_date"}
    missing_columns = required_columns - set(valuation_df.columns)
    if missing_columns:
        raise ValueError(f"Missing required valuation columns: {sorted(missing_columns)}")

    float_columns = {"pe1", "pe2", "dividend_yield1", "dividend_yield2"}
    records = []
    for row in valuation_df.copy().itertuples(index=False):
        record: dict = {
            "index_code": str(getattr(row, "index_code")).strip(),
            "trade_date": pd.Timestamp(getattr(row, "trade_date")).date().isoformat(),
        }
        for column in float_columns:
            if hasattr(row, column):
                value = getattr(row, column)
                if pd.notna(value):
                    record[column] = float(value)
        records.append(record)

    if records:
        client.table("index_valuation_history").upsert(records, on_conflict="index_code,trade_date").execute()
    return len(records)


def fetch_index_valuations(client: Client, index_code: str) -> pd.DataFrame:
    """Fetch all valuation rows for one index (index_valuation_history)."""
    query_builder = (
        client.table("index_valuation_history")
        .select("index_code, trade_date, pe1, pe2, dividend_yield1, dividend_yield2")
        .eq("index_code", str(index_code).strip())
        .order("trade_date")
    )
    data = _fetch_all_rows(query_builder)
    if not data:
        return pd.DataFrame(columns=["index_code", "trade_date", "pe1", "pe2", "dividend_yield1", "dividend_yield2"])

    df = pd.DataFrame(data)
    df["index_code"] = df["index_code"].astype(str)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for column in ["pe1", "pe2", "dividend_yield1", "dividend_yield2"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["trade_date", "dividend_yield1"]).reset_index(drop=True)


def upsert_daily_factors(client: Client, factors_df: pd.DataFrame) -> int:
    """Upsert daily strategy factors into fund_daily_factors. Returns row count."""
    required_columns = {"fund_code", "trade_date"}
    missing_columns = required_columns - set(factors_df.columns)
    if missing_columns:
        raise ValueError(f"Missing required factor columns: {sorted(missing_columns)}")

    bool_columns = {"signal_a", "signal_b"}
    float_columns = {
        "dividend_yield",
        "annualized_vol",
        "max_drawdown",
        "dividend_yield_pctile",
        "spread",
        "spread_pctile",
        "dy_vol_ratio_pctile",
        "drawdown_pctile",
        "vol_pctile",
        "score_a",
        "score_b",
    }

    records = []
    for row in factors_df.copy().itertuples(index=False):
        record: dict = {
            "fund_code": normalize_fund_code(getattr(row, "fund_code")),
            "trade_date": pd.Timestamp(getattr(row, "trade_date")).date().isoformat(),
        }
        for column in float_columns:
            if hasattr(row, column):
                value = getattr(row, column)
                if pd.notna(value):
                    record[column] = float(value)
        for column in bool_columns:
            if hasattr(row, column):
                value = getattr(row, column)
                if pd.notna(value):
                    record[column] = bool(value)
        records.append(record)

    if records:
        client.table("fund_daily_factors").upsert(records, on_conflict="fund_code,trade_date").execute()
    return len(records)


def delete_fund_daily_factors(client: Client, fund_code: str) -> None:
    """Delete all daily factor rows for one fund.

    因子重算前先清理旧口径数据，避免旧口径（如分红率）残留污染。
    """
    client.table("fund_daily_factors").delete().eq("fund_code", normalize_fund_code(fund_code)).execute()


def get_watermark(client: Client, entity_type: str, entity_code: str) -> pd.DataFrame:
    """Read the sync watermark for one entity. Returns an empty DataFrame if absent."""
    response = (
        client.table("sync_watermarks")
        .select("entity_type, entity_code, last_date, source, updated_at")
        .eq("entity_type", entity_type)
        .eq("entity_code", normalize_fund_code(entity_code) if entity_type == "fund" else str(entity_code))
        .execute()
    )
    data = response.data or []
    if not data:
        return pd.DataFrame(columns=["entity_type", "entity_code", "last_date", "source", "updated_at"])

    df = pd.DataFrame(data)
    df["last_date"] = pd.to_datetime(df["last_date"], errors="coerce")
    df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce")
    return df


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
    client.table("sync_watermarks").upsert([row], on_conflict="entity_type,entity_code").execute()


def list_watermarks(client: Client) -> pd.DataFrame:
    """List all sync watermarks (used by the dashboard to show refresh state)."""
    query_builder = client.table("sync_watermarks").select("entity_type, entity_code, last_date, source, updated_at").order("updated_at", desc=True)
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
    """Fetch all rate history for one rate code."""
    query_builder = (
        client.table("macro_rates_history")
        .select("rate_code, rate_date, rate_value")
        .eq("rate_code", rate_code)
        .order("rate_date")
    )
    data = _fetch_all_rows(query_builder)
    if not data:
        return pd.DataFrame(columns=["rate_code", "rate_date", "rate_value"])

    df = pd.DataFrame(data)
    df["rate_code"] = df["rate_code"].astype(str)
    df["rate_date"] = pd.to_datetime(df["rate_date"], errors="coerce")
    df["rate_value"] = pd.to_numeric(df["rate_value"], errors="coerce")
    return df.dropna(subset=["rate_date", "rate_value"]).reset_index(drop=True)


def fetch_daily_factors(client: Client, fund_code: str) -> pd.DataFrame:
    """Fetch all daily factors for one fund (fund_daily_factors)."""
    query_builder = (
        client.table("fund_daily_factors")
        .select(
            "fund_code, trade_date, dividend_yield, annualized_vol, max_drawdown, "
            "dividend_yield_pctile, spread, spread_pctile, dy_vol_ratio_pctile, "
            "drawdown_pctile, vol_pctile, score_a, signal_a, score_b, signal_b"
        )
        .eq("fund_code", normalize_fund_code(fund_code))
        .order("trade_date")
    )
    data = _fetch_all_rows(query_builder)
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    df["fund_code"] = df["fund_code"].astype(str).apply(normalize_fund_code)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for column in [
        "dividend_yield",
        "annualized_vol",
        "max_drawdown",
        "dividend_yield_pctile",
        "spread",
        "spread_pctile",
        "dy_vol_ratio_pctile",
        "drawdown_pctile",
        "vol_pctile",
        "score_a",
        "score_b",
    ]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["trade_date"]).reset_index(drop=True)


def list_fund_profiles(client: Client) -> pd.DataFrame:
    query_builder = (
        client.table("fund_profiles")
        .select("fund_code, fund_name, fund_type, tracking_index, created_at")
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


def fetch_latest_sync_job(client: Client) -> pd.DataFrame:
    response = (
        client.table("sync_jobs")
        .select("log_id, job_name, status, message, row_count, executed_at")
        .order("executed_at", desc=True)
        .limit(1)
        .execute()
    )
    data = response.data or []
    if not data:
        return pd.DataFrame(columns=["log_id", "job_name", "status", "message", "row_count", "executed_at"])

    return pd.DataFrame(data)