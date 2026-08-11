"""指数行情/估值抓取：中证官网（csindex）指数行情 + 推导股息率。

说明：
- `fetch_index_daily_history`：指数日行情全历史（须显式传起止日期）。
- `derive_index_dividend_yield`：用「全收益/价格指数比」推导历史股息率（近似口径），
  由 _refresh_valuation 只填官方覆盖之前的日期并落库（source='derived'），随官方累积自动替换。
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import akshare as ak
import pandas as pd

SOURCE = "csindex"
DERIVED_SOURCE = "derived"

# 推导股息率的滚动窗口（交易日）
DERIVE_WINDOW_DAYS = 252


def _tri_index_code(index_code: str) -> str:
    """由价格指数代码推导其全收益（TRI）指数代码。

    中证指数里 H30269（价格）↔ H20269（全收益）为一对：把 "H3" 换成 "H2"。
    仅支持该 H3xxx/H2xxx 配对模式；其它模式需显式配置，否则报错。
    """
    code = str(index_code).strip().upper()
    if code.startswith("H3"):
        return "H2" + code[2:]
    raise ValueError(f"无法从 {index_code} 推导全收益指数代码（仅支持 H3xxx↔H2xxx 模式）。")


@lru_cache(maxsize=16)
def derive_index_dividend_yield(index_code: str, *, window_days: int = DERIVE_WINDOW_DAYS) -> pd.DataFrame:
    """用「全收益/价格指数比」推导历史股息率（近似口径，仅内存使用，不落库）。

    口径：每个交易日的股息贡献 = TRI 日收益 − 价格日收益；过去 window_days 个
    交易日的累计股息贡献即近似年化股息率（%）。返回：
    index_code / trade_date / dividend_yield1 / source('derived')。

    :param index_code: 价格指数代码（如 H30269）。
    :param window_days: 滚动窗口（交易日），默认 252。
    """
    price_df = ak.stock_zh_index_hist_csindex(symbol=index_code, start_date="20000101", end_date=date.today().strftime("%Y%m%d"))
    tri_code = _tri_index_code(index_code)
    tri_df = ak.stock_zh_index_hist_csindex(symbol=tri_code, start_date="20000101", end_date=date.today().strftime("%Y%m%d"))

    price = price_df[["日期", "收盘"]].rename(columns={"日期": "trade_date", "收盘": "price"}).copy()
    tri = tri_df[["日期", "收盘"]].rename(columns={"日期": "trade_date", "收盘": "tri"}).copy()

    merged = pd.merge(price, tri, on="trade_date", how="inner").sort_values("trade_date").reset_index(drop=True)
    merged["trade_date"] = pd.to_datetime(merged["trade_date"], errors="coerce")
    merged["price"] = pd.to_numeric(merged["price"], errors="coerce")
    merged["tri"] = pd.to_numeric(merged["tri"], errors="coerce")
    merged = merged.dropna(subset=["trade_date", "price", "tri"])

    merged["tri_ret"] = merged["tri"].pct_change()
    merged["price_ret"] = merged["price"].pct_change()
    merged["div_contrib"] = merged["tri_ret"] - merged["price_ret"]
    merged["dividend_yield1"] = merged["div_contrib"].rolling(window_days).sum() * 100.0

    result = merged[["trade_date", "dividend_yield1"]].dropna().copy()
    result["index_code"] = str(index_code).strip()
    result["source"] = DERIVED_SOURCE
    return result[["index_code", "trade_date", "dividend_yield1", "source"]].reset_index(drop=True)


# 全收益指数代码（index_type='total_return'），其余为 'price'
TOTAL_RETURN_CODES = frozenset({"H20269", "H00300", "000300S"})


def fetch_index_daily_history(index_code: str, *, start_date: str = "20000101", end_date: str | None = None) -> pd.DataFrame:
    """抓取指数日行情全历史（csindex 官方，须显式传起止日期）。

    :return: DataFrame(index_code, trade_date, open, high, low, close, change_pct, volume, amount, index_type)
    """
    end = end_date or date.today().strftime("%Y%m%d")
    # csindex 对部分代码偶发返回空，akshare 会抛 Length mismatch；加健壮处理 + 重试
    raw_df = None
    for _ in range(3):
        try:
            raw_df = ak.stock_zh_index_hist_csindex(symbol=index_code, start_date=start_date, end_date=end)
            if raw_df is not None and not raw_df.empty:
                break
        except Exception:  # noqa: BLE001
            raw_df = None
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(
            columns=["index_code", "trade_date", "open", "high", "low", "close",
                     "change_pct", "volume", "amount", "index_type"]
        )

    rename = {
        "日期": "trade_date", "开盘": "open", "最高": "high", "最低": "low",
        "收盘": "close", "涨跌幅": "change_pct", "成交量": "volume", "成交金额": "amount",
    }
    df = raw_df[list(rename.keys())].rename(columns=rename).copy()
    df["index_code"] = str(index_code).strip()
    df["index_type"] = "total_return" if str(index_code).strip().upper() in TOTAL_RETURN_CODES else "price"
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for column in ("open", "high", "low", "close", "change_pct", "volume", "amount"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = (
        df.dropna(subset=["trade_date", "close"])
        .drop_duplicates(subset=["index_code", "trade_date"], keep="last")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    return df[["index_code", "trade_date", "open", "high", "low", "close",
               "change_pct", "volume", "amount", "index_type"]]
