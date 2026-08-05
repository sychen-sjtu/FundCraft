"""正式 Supabase 同步任务：把本地快照同步到云数据库并回读复核。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from src.config import load_supabase_settings, supabase_settings_ready
from src.indicators.fund_metrics import compute_fund_metrics
from src.storage.local_store import find_latest_raw_snapshot, load_fund_snapshots
from src.storage.supabase_store import (
    create_supabase_client,
    fetch_nav_history,
    insert_sync_log,
    upsert_fund_profiles,
    upsert_nav_history,
)


def build_database_summary(client, fund_codes: list[str]) -> pd.DataFrame:
    rows = []
    for code in fund_codes:
        nav_df = fetch_nav_history(client, code)
        if nav_df.empty:
            continue
        metrics = compute_fund_metrics(nav_df)
        rows.append(
            {
                "fund_code": metrics.fund_code,
                "start_date": metrics.start_date,
                "end_date": metrics.end_date,
                "row_count": metrics.row_count,
                "start_unit_nav": metrics.start_unit_nav,
                "end_unit_nav": metrics.end_unit_nav,
                "cumulative_return_pct": round(metrics.cumulative_return_pct, 4),
                "max_drawdown_pct": round(metrics.max_drawdown_pct, 4),
                "annualized_volatility_pct": round(metrics.annualized_volatility_pct, 4),
            }
        )

    return pd.DataFrame(rows).sort_values("fund_code").reset_index(drop=True) if rows else pd.DataFrame()


def run_supabase_sync(project_root: Path | None = None, *, secret_password: str | None = None) -> Path | None:
    """执行一次正式同步，返回回读汇总 CSV 路径；未配置 Supabase 时返回 None。"""
    root = project_root or Path(__file__).resolve().parents[2]
    settings = load_supabase_settings(root, secret_password=secret_password)
    if not supabase_settings_ready(settings):
        print("Supabase settings are missing. Fill .streamlit/secrets.toml first.")
        return None

    client = create_supabase_client(settings)

    snapshot_dir = find_latest_raw_snapshot(root)
    raw_dataframes = load_fund_snapshots(snapshot_dir)
    fund_codes = sorted({str(df["fund_code"].iloc[0]) for df in raw_dataframes})

    total_rows = 0
    for df in raw_dataframes:
        upsert_nav_history(client, df)
        total_rows += int(len(df))

    upsert_fund_profiles(client, [{"fund_code": code} for code in fund_codes])

    summary_df = build_database_summary(client, fund_codes)

    processed_dir = root / "data" / "processed" / "stage4_supabase"
    processed_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = processed_dir / f"summary_{timestamp}.csv"
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")

    insert_sync_log(
        client,
        job_name="stage4_supabase_sync",
        status="success",
        message=f"Synced {len(fund_codes)} funds from {snapshot_dir.name}",
        row_count=total_rows,
    )

    print(f"Snapshot directory: {snapshot_dir}")
    print(f"Synced funds: {', '.join(fund_codes)}")
    print(f"Saved summary CSV to: {summary_path}")
    if not summary_df.empty:
        print("Summary metrics read back from Supabase:")
        print(summary_df.to_string(index=False))
    return summary_path


if __name__ == "__main__":
    run_supabase_sync()
