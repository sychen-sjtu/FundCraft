from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import akshare as ak


@dataclass(frozen=True)
class FundSpec:
    code: str


def normalize_fund_code(code: str | int) -> str:
    """Normalize a fund code to a 6-character string identifier."""
    normalized = str(code).strip()
    return normalized.zfill(6) if normalized.isdigit() and len(normalized) < 6 else normalized


def fetch_fund_nav_history(code: str) -> pd.DataFrame:
    """Fetch and normalize the unit net value history for one fund."""
    code = normalize_fund_code(code)
    raw_df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    if raw_df.empty:
        raise ValueError(f"No fund data returned for {code}")

    if raw_df.shape[1] < 2:
        raise ValueError(f"Unexpected fund data shape for {code}: {raw_df.shape}")

    normalized = raw_df.iloc[:, :3].copy()
    normalized.columns = ["nav_date", "unit_nav", "daily_return"][: normalized.shape[1]]

    normalized["nav_date"] = pd.to_datetime(normalized["nav_date"], errors="coerce")
    normalized["unit_nav"] = pd.to_numeric(normalized["unit_nav"], errors="coerce")
    if "daily_return" in normalized.columns:
        normalized["daily_return"] = pd.to_numeric(normalized["daily_return"], errors="coerce")

    normalized = normalized.dropna(subset=["nav_date", "unit_nav"]).sort_values("nav_date").reset_index(drop=True)
    normalized.insert(0, "fund_code", code)
    return normalized


def fetch_multiple_funds(codes: Iterable[str]) -> list[pd.DataFrame]:
    return [fetch_fund_nav_history(code) for code in codes]


def save_fund_snapshots(dataframes: Iterable[pd.DataFrame], output_dir: Path) -> Path:
    """Save one CSV per fund and a combined manifest under the output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows: list[pd.DataFrame] = []
    for df in dataframes:
        code = normalize_fund_code(df["fund_code"].iloc[0])
        file_path = output_dir / f"{code}.csv"
        df.to_csv(file_path, index=False, encoding="utf-8-sig")

        summary = pd.DataFrame(
            [
                {
                    "fund_code": code,
                    "row_count": int(len(df)),
                    "start_date": df["nav_date"].min().date().isoformat(),
                    "end_date": df["nav_date"].max().date().isoformat(),
                    "latest_unit_nav": float(df["unit_nav"].iloc[-1]),
                }
            ]
        )
        manifest_rows.append(summary)

    manifest = pd.concat(manifest_rows, ignore_index=True)
    manifest_path = output_dir / "manifest.csv"
    manifest.to_csv(manifest_path, index=False, encoding="utf-8-sig")
    return manifest_path