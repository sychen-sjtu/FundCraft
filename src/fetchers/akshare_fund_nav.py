from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import akshare as ak
import pandas as pd
import requests


@dataclass(frozen=True)
class FundSpec:
    code: str


def normalize_fund_code(code: str | int) -> str:
    """Normalize a fund code to a 6-character string identifier."""
    normalized = str(code).strip()
    return normalized.zfill(6) if normalized.isdigit() and len(normalized) < 6 else normalized


# 东财历史净值接口（f10/lsjz）。akshare 的 fund_open_fund_info_em 不支持日期过滤、
# 每次只能拉全历史；这里直接调其底层东财接口，支持 startDate/endDate，实现真正的增量拉取。
_EM_LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"
_EM_LSJZ_HEADERS = {
    "Referer": "http://fundf10.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
}
# 东财 f10/lsjz 每页最多返回 20 条（请求更大也按 20 截断），分页取全。
_EM_LSJZ_PAGE_SIZE = 20


def _fetch_nav_range_em(code: str, start_date: str, end_date: str | None = None) -> pd.DataFrame:
    """从东财 f10/lsjz 按日期区间拉取单只基金单位净值（增量用）。

    返回列（与 fetch_fund_nav_history 一致，未含 fund_code / adjusted_nav）：
    nav_date / unit_nav / daily_return(%)。区间无数据返回空表；网络/接口异常抛错（调用方回退全量）。
    """
    code = normalize_fund_code(code)
    end_date = end_date or datetime.now().strftime("%Y-%m-%d")
    rows: list[dict] = []
    page_index = 1
    while True:
        params = {
            "fundCode": code,
            "pageIndex": page_index,
            "pageSize": _EM_LSJZ_PAGE_SIZE,
            "startDate": start_date,
            "endDate": end_date,
        }
        resp = requests.get(_EM_LSJZ_URL, params=params, headers=_EM_LSJZ_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        page = (data.get("Data") or {}).get("LSJZList") or []
        rows.extend(page)
        total = int(data.get("TotalCount") or 0)
        if not page or len(page) < _EM_LSJZ_PAGE_SIZE or len(rows) >= total:
            break
        page_index += 1

    if not rows:
        return pd.DataFrame(columns=["nav_date", "unit_nav", "daily_return"])
    df = pd.DataFrame(rows)
    df["nav_date"] = pd.to_datetime(df["FSRQ"], errors="coerce")
    df["unit_nav"] = pd.to_numeric(df["DWJZ"], errors="coerce")
    df["daily_return"] = pd.to_numeric(df["JZZZL"], errors="coerce")
    df = df.dropna(subset=["nav_date", "unit_nav"]).sort_values("nav_date").reset_index(drop=True)
    return df[["nav_date", "unit_nav", "daily_return"]]


def fetch_fund_nav_history(code: str, *, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    """Fetch and normalize the unit net value history for one fund.

    :param start_date/end_date: ISO 日期（YYYY-MM-DD）。给定 start_date 时走东财 lsjz
        增量接口（只拉该区间，网络增量）；不给则用 akshare fund_open_fund_info_em 拉全历史。
    """
    code = normalize_fund_code(code)
    if start_date:
        range_df = _fetch_nav_range_em(code, start_date, end_date)
        if not range_df.empty:
            range_df = range_df.copy()
            range_df.insert(0, "fund_code", code)
        return range_df

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


def derive_adjusted_nav(nav_df: pd.DataFrame, *, prev_adjusted_nav: float | None = None) -> pd.DataFrame:
    """由单位净值 + 日增长率推导复权净值（红利再投资口径）。

    口径（docs/基金净值数据与走势对比说明.md）：
        adjusted_nav[t] = adjusted_nav[t-1] × (1 + daily_return[t]/100)，起点取首个 unit_nav；
    日增长率为空/NaN 时保持前值（跳过该日）。

    :param nav_df: 需含 nav_date / unit_nav / daily_return(%)
    :param prev_adjusted_nav: 增量窗口的复权接续锚点（窗口前一日已入库的 adjusted_nav）。
        提供时首个交易日按 prev_adjusted_nav × (1 + r) 接续，避免增量窗口内重算导致复权断裂；
        不提供（全历史）时起点取首个 unit_nav。
    :return: 入参副本上增加 adjusted_nav 列（按 nav_date 升序）
    """
    df = nav_df.copy().sort_values("nav_date").reset_index(drop=True)
    returns = pd.to_numeric(df.get("daily_return"), errors="coerce") / 100.0
    unit = pd.to_numeric(df["unit_nav"], errors="coerce")

    adjusted: list[float | None] = []
    prev: float | None = None
    for r, u in zip(returns, unit):
        if prev is None:
            if prev_adjusted_nav is not None:
                prev = float(prev_adjusted_nav)
            else:
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