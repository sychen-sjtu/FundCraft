from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.config import load_supabase_settings, supabase_settings_ready
from src.fetchers.akshare_fund_nav import normalize_fund_code
from src.indicators.fund_metrics import compute_fund_metrics
from src.storage.supabase_store import (
    create_supabase_client,
    fetch_nav_history,
    insert_sync_log,
    upsert_fund_profiles,
    upsert_nav_history,
)


def find_latest_raw_snapshot(root: Path) -> Path:
    raw_root = root / "data" / "raw" / "fund_nav"
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw fund NAV directory not found: {raw_root}")

    snapshot_dirs = [path for path in raw_root.iterdir() if path.is_dir()]
    if not snapshot_dirs:
        raise FileNotFoundError(f"No snapshot directories found under: {raw_root}")

    return sorted(snapshot_dirs)[-1]


def load_raw_snapshot(snapshot_dir: Path) -> list[pd.DataFrame]:
    dataframes: list[pd.DataFrame] = []
    for csv_path in sorted(snapshot_dir.glob("*.csv")):
        if csv_path.name == "manifest.csv":
            continue

        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        if "fund_code" in df.columns:
            df["fund_code"] = df["fund_code"].astype(str).apply(normalize_fund_code)
        df["nav_date"] = pd.to_datetime(df["nav_date"], errors="coerce")
        df["unit_nav"] = pd.to_numeric(df["unit_nav"], errors="coerce")
        if "daily_return" in df.columns:
            df["daily_return"] = pd.to_numeric(df["daily_return"], errors="coerce")
        df = df.dropna(subset=["fund_code", "nav_date", "unit_nav"])
        dataframes.append(df)

    if not dataframes:
        raise FileNotFoundError(f"No fund CSV files found in: {snapshot_dir}")

    return dataframes


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


def main() -> None:
    settings = load_supabase_settings(PROJECT_ROOT)
    if not supabase_settings_ready(settings):
        print("Supabase settings are missing. Fill .streamlit/secrets.toml first.")
        return

    client = create_supabase_client(settings)

    snapshot_dir = find_latest_raw_snapshot(PROJECT_ROOT)
    raw_dataframes = load_raw_snapshot(snapshot_dir)
    fund_codes = sorted({str(df["fund_code"].iloc[0]) for df in raw_dataframes})

    total_rows = 0
    for df in raw_dataframes:
        upsert_nav_history(client, df)
        total_rows += int(len(df))

    upsert_fund_profiles(client, fund_codes)

    summary_df = build_database_summary(client, fund_codes)

    processed_dir = PROJECT_ROOT / "data" / "processed" / "stage4_supabase"
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


if __name__ == "__main__":
    main()