"""本地原始/处理后文件的读取封装，供分析和同步等正式任务复用。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.fetchers.akshare_fund_nav import normalize_fund_code


def find_latest_raw_snapshot(root: Path) -> Path:
    """返回 data/raw/fund_nav 下最新的快照目录。"""
    raw_root = root / "data" / "raw" / "fund_nav"
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw fund NAV directory not found: {raw_root}")

    snapshot_dirs = [path for path in raw_root.iterdir() if path.is_dir()]
    if not snapshot_dirs:
        raise FileNotFoundError(f"No snapshot directories found under: {raw_root}")

    return sorted(snapshot_dirs)[-1]


def load_fund_snapshots(snapshot_dir: Path, *, include_daily_return: bool = True) -> list[pd.DataFrame]:
    """读取快照目录下所有基金 CSV，统一字段并规范化基金代码。"""
    dataframes: list[pd.DataFrame] = []
    for csv_path in sorted(snapshot_dir.glob("*.csv")):
        if csv_path.name == "manifest.csv":
            continue

        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        if "fund_code" in df.columns:
            df["fund_code"] = df["fund_code"].astype(str).str.strip().apply(normalize_fund_code)
        df["nav_date"] = pd.to_datetime(df["nav_date"], errors="coerce")
        df["unit_nav"] = pd.to_numeric(df["unit_nav"], errors="coerce")
        if include_daily_return and "daily_return" in df.columns:
            df["daily_return"] = pd.to_numeric(df["daily_return"], errors="coerce")

        required_columns = {"fund_code", "nav_date", "unit_nav"}
        missing_columns = required_columns - set(df.columns)
        if missing_columns:
            raise ValueError(f"Snapshot file missing required columns: {sorted(missing_columns)}")

        df = df.dropna(subset=["fund_code", "nav_date", "unit_nav"])
        dataframes.append(df)

    if not dataframes:
        raise FileNotFoundError(f"No fund CSV files found in: {snapshot_dir}")

    return dataframes
