"""宏观利率抓取：从 bond_zh_us_rate 提取中国10年期国债收益率（cn_10y）。"""

from __future__ import annotations

import akshare as ak
import pandas as pd

# 当前只存一个口径：中国10年期国债收益率
CN_10Y_COLUMN = "中国国债收益率10年"
RATE_CODE = "cn_10y"
SOURCE = "bond_zh_us_rate"


def fetch_cn_10y_rate(start_date: str = "20000101") -> pd.DataFrame:
    """抓取中国10年期国债收益率的完整历史。

    `ak.bond_zh_us_rate` 一次返回中国/美国各期限国债收益率与 GDP 增速，
    这里只提取 cn_10y。返回字段：rate_code / rate_date / rate_value / source。

    :param start_date: 起始日期（YYYYMMDD），默认 2000-01-01 尽量覆盖全历史。
    :return: 规范化后的利率 DataFrame（可为空）。
    """
    raw_df = ak.bond_zh_us_rate(start_date=start_date)
    if raw_df is None or raw_df.empty or CN_10Y_COLUMN not in raw_df.columns:
        return pd.DataFrame(columns=["rate_code", "rate_date", "rate_value", "source"])

    rate_df = raw_df[["日期", CN_10Y_COLUMN]].copy()
    rate_df.columns = ["rate_date", "rate_value"]

    rate_df["rate_code"] = RATE_CODE
    rate_df["source"] = SOURCE
    rate_df["rate_date"] = pd.to_datetime(rate_df["rate_date"], errors="coerce")
    rate_df["rate_value"] = pd.to_numeric(rate_df["rate_value"], errors="coerce")

    rate_df = rate_df.dropna(subset=["rate_date", "rate_value"])
    rate_df = (
        rate_df.drop_duplicates(subset=["rate_code", "rate_date"], keep="last")
        .sort_values("rate_date")
        .reset_index(drop=True)
    )
    return rate_df[["rate_code", "rate_date", "rate_value", "source"]]
