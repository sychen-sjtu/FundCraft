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


def upsert_fund_profiles(client: Client, fund_codes: Iterable[str]) -> None:
    rows = [{"fund_code": normalize_fund_code(code)} for code in fund_codes]
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


def fetch_nav_history(client: Client, fund_code: str) -> pd.DataFrame:
    query_builder = (
        client.table("fund_nav_history")
        .select("fund_code, nav_date, unit_nav, daily_return")
        .eq("fund_code", normalize_fund_code(fund_code))
        .order("nav_date")
    )
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


def list_fund_profiles(client: Client) -> pd.DataFrame:
    query_builder = client.table("fund_profiles").select("fund_code").order("fund_code")
    data = _fetch_all_rows(query_builder)
    if not data:
        return pd.DataFrame(columns=["fund_code"])

    df = pd.DataFrame(data)
    df["fund_code"] = df["fund_code"].astype(str).apply(normalize_fund_code)
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