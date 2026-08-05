"""基金分红抓取：通过 fund_fh_em 按年批量查询后过滤目标基金。"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

import akshare as ak
import pandas as pd

from src.fetchers.akshare_fund_nav import normalize_fund_code

# 分红抓取的起始年份（保守覆盖，008163 自 2023-12 起分红，留足余量）
DIVIDEND_FETCH_START_YEAR = 2015


def _resolve_fund_types(target_codes: set[str]) -> set[str]:
    """从 fund_name_em 解析目标基金的「基金类型」集合，用于缩小分红查询范围。

    fund_fh_em 若用 typ=""（全部类型）会翻页拉取全市场分红，非常慢；只查询
    目标基金所属类型可大幅减少请求量。
    """
    types: set[str] = set()
    try:
        names_df = ak.fund_name_em()
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: fund_name_em failed, fallback to typ='' : {exc}")
        return types

    if names_df.empty or not {"基金代码", "基金类型"}.issubset(names_df.columns):
        return types

    sub = names_df[names_df["基金代码"].astype(str).str.strip().apply(normalize_fund_code).isin(target_codes)]
    types = {str(value).strip() for value in sub["基金类型"].dropna() if str(value).strip()}
    return types


def fetch_fund_dividends(
    codes: Iterable[str],
    *,
    start_year: int = DIVIDEND_FETCH_START_YEAR,
    end_year: int | None = None,
) -> pd.DataFrame:
    """抓取指定基金的历史分红记录。

    说明：`ak.fund_fh_em` 按"年份 + 基金类型"批量返回分红明细，无法按单基金
    直接查询。这里先解析目标基金的类型集合，再逐年逐类型拉取后过滤，避免用
    typ="" 全量翻页。返回字段统一为：fund_code / ex_date / dividend_per_unit。

    :param codes: 目标基金代码列表。
    :param start_year: 起始查询年份。
    :param end_year: 结束查询年份，默认当前年份。
    :return: 规范化后的分红 DataFrame（可为空）。
    """
    target_codes = {normalize_fund_code(code) for code in codes}
    end_year = end_year or datetime.now().year

    fund_types = _resolve_fund_types(target_codes)
    query_types = sorted(fund_types) if fund_types else [""]

    frames: list[pd.DataFrame] = []
    for year in range(start_year, end_year + 1):
        for fund_type in query_types:
            try:
                year_df = ak.fund_fh_em(year=str(year), typ=fund_type)
            except Exception as exc:  # noqa: BLE001 - 单年/单类型失败不阻塞整体
                print(f"WARN: fund_fh_em year={year} typ={fund_type!r} failed: {exc}")
                continue

            if year_df is None or year_df.empty:
                continue

            if "基金代码" not in year_df.columns:
                continue

            year_df = year_df.copy()
            year_df["基金代码"] = year_df["基金代码"].astype(str).str.strip().apply(normalize_fund_code)
            sub = year_df[year_df["基金代码"].isin(target_codes)]
            if not sub.empty:
                frames.append(sub)

    if not frames:
        return pd.DataFrame(columns=["fund_code", "ex_date", "dividend_per_unit"])

    raw = pd.concat(frames, ignore_index=True)

    # 列名映射：分红接口返回 除息日期 / 分红（每份，元）
    dividend_df = raw[["基金代码", "除息日期", "分红"]].copy()
    dividend_df.columns = ["fund_code", "ex_date", "dividend_per_unit"]

    dividend_df["fund_code"] = dividend_df["fund_code"].astype(str).apply(normalize_fund_code)
    dividend_df["ex_date"] = pd.to_datetime(dividend_df["ex_date"], errors="coerce")
    dividend_df["dividend_per_unit"] = pd.to_numeric(dividend_df["dividend_per_unit"], errors="coerce")
    dividend_df = dividend_df.dropna(subset=["fund_code", "ex_date", "dividend_per_unit"])

    # 同一 (fund_code, ex_date) 出现多次时取最大分红金额，避免主键冲突
    dividend_df = (
        dividend_df.groupby(["fund_code", "ex_date"], as_index=False)["dividend_per_unit"]
        .max()
        .sort_values(["fund_code", "ex_date"])
        .reset_index(drop=True)
    )
    return dividend_df
