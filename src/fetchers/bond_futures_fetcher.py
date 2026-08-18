"""国债期货（TF/T）抓取：新浪主力连续日线 + 当日分钟线。

数据源（akshare 1.18.81 实测）：
- 日线：``ak.futures_main_sina(symbol, start_date, end_date)`` → 主力连续日线（含前收，
  支持日期过滤，适合增量同步）。返回 日期/开盘价/最高价/最低价/收盘价/成交量/持仓量/动态结算价。
- 分钟线：``ak.futures_zh_minute_sina(symbol, period='1')`` → 当日全部 1 分钟 bar（盘中实时，
  不落库）。返回 datetime/open/high/low/close/volume/hold。

⚠️ 主力连续为换月跳空口径（TF0/T0 直接使用，接受跳空，仅作买卖信号语境）。
"""

from __future__ import annotations

import akshare as ak
import pandas as pd

# (新浪 symbol, 落库 rate_code, 展示名)
# TF=5年期国债期货（主力），T=10年期国债期货（辅助信号）；期限贴合 3-5 年国开债债基。
BOND_FUTURES = [
    ("TF0", "bond_futures_tf", "TF(5年)"),
    ("T0", "bond_futures_t", "T(10年)"),
]

SOURCE = "sina"


def rate_code_for(symbol: str) -> str:
    """新浪 symbol → 落库 rate_code（未知 symbol 兜底生成）。"""
    for sym, code, _name in BOND_FUTURES:
        if sym == symbol:
            return code
    return f"bond_futures_{symbol}"


def fetch_bond_futures_daily(symbol: str, start_date: str = "20170101") -> pd.DataFrame:
    """抓取国债期货主力连续日线（收盘价口径），返回 rate_code/trade_date/rate_value/source。

    :param symbol: 新浪 symbol，如 TF0 / T0。
    :param start_date: 起始日期（YYYYMMDD），默认 2017-01-01（TF/T 上市起点）。
    :return: 规范化后的日线 DataFrame（可为空）。
    """
    raw_df = ak.futures_main_sina(symbol=symbol, start_date=start_date, end_date="22220101")
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=["rate_code", "trade_date", "rate_value", "source"])

    frame = raw_df[["日期", "收盘价"]].copy()
    frame.columns = ["trade_date", "rate_value"]
    frame["rate_code"] = rate_code_for(symbol)
    frame["source"] = SOURCE
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["rate_value"] = pd.to_numeric(frame["rate_value"], errors="coerce")

    frame = frame.dropna(subset=["trade_date", "rate_value"])
    frame = (
        frame.drop_duplicates(subset=["rate_code", "trade_date"], keep="last")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    return frame[["rate_code", "trade_date", "rate_value", "source"]]


def fetch_bond_futures_intraday(symbol: str) -> pd.DataFrame:
    """抓取国债期货主力连续当日分钟线（1 分钟，盘中实时）。

    :param symbol: 新浪 symbol，如 TF0 / T0。
    :return: DataFrame(datetime, open, high, low, close, volume, hold)，升序（可为空）。
    """
    raw_df = ak.futures_zh_minute_sina(symbol=symbol, period="1")
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume", "hold"])

    frame = raw_df.copy()
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["datetime", "close"])
    return frame.sort_values("datetime").reset_index(drop=True)
