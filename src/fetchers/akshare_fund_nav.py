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


def derive_adjusted_nav(nav_df: pd.DataFrame) -> pd.DataFrame:
    """由单位净值 + 日增长率推导复权净值（红利再投资口径）。

    口径（docs/基金净值数据与走势对比说明.md）：
        adjusted_nav[t] = adjusted_nav[t-1] × (1 + daily_return[t]/100)，起点取首个 unit_nav；
    日增长率为空/NaN 时保持前值（跳过该日）。

    :param nav_df: 需含 nav_date / unit_nav / daily_return(%)
    :return: 入参副本上增加 adjusted_nav 列（按 nav_date 升序）
    """
    df = nav_df.copy().sort_values("nav_date").reset_index(drop=True)
    returns = pd.to_numeric(df.get("daily_return"), errors="coerce") / 100.0
    unit = pd.to_numeric(df["unit_nav"], errors="coerce")

    adjusted: list[float | None] = []
    prev: float | None = None
    for r, u in zip(returns, unit):
        if prev is None:
            prev = float(u) if pd.notna(u) else None
            adjusted.append(prev)
            continue
        if pd.notna(r):
            prev = prev * (1.0 + float(r))
        adjusted.append(prev)

    df["adjusted_nav"] = adjusted
    return df


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


def fetch_fund_profiles(codes: Iterable[str]) -> list[dict]:
    """Fetch fund basic info (name / type / tracking index) for the given codes.

    name/type 来自 fund_name_em（一次全量），tracking_index 来自
    fund_individual_basic_info_xq（逐只，尽力而为，失败则留空）。
    """
    target_codes = sorted({normalize_fund_code(code) for code in codes})

    names_df = pd.DataFrame()
    try:
        names_df = ak.fund_name_em()
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: fund_name_em failed: {exc}")

    profiles: list[dict] = []
    for code in target_codes:
        profile: dict = {"fund_code": code}

        if not names_df.empty and "基金代码" in names_df.columns:
            matched = names_df[names_df["基金代码"].astype(str).str.strip() == code]
            if not matched.empty:
                first = matched.iloc[0]
                profile["fund_name"] = str(first.get("基金简称", "")).strip()
                profile["fund_type"] = str(first.get("基金类型", "")).strip()
                # 场内 ETF 判定（启发式：类型含 ETF/场内 字样；当前基金均为场外 → False）
                fund_type_upper = str(profile.get("fund_type", "")).upper()
                profile["is_etf"] = ("ETF" in fund_type_upper) or ("场内" in fund_type_upper)

        try:
            info_df = ak.fund_individual_basic_info_xq(symbol=code)
            if not info_df.empty and "item" in info_df.columns and "value" in info_df.columns:
                info_map = dict(zip(info_df["item"].astype(str), info_df["value"].astype(str)))
                benchmark = info_map.get("业绩比较基准", "").strip()
                if benchmark:
                    profile["tracking_index"] = benchmark
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: fund_individual_basic_info_xq {code} failed: {exc}")

        profiles.append(profile)

    return profiles