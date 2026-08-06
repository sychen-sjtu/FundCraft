"""指数估值抓取：从中证官网 stock_zh_index_value_csindex 拉取指数 PE / 股息率。

说明：
- `fetch_index_valuations`：官方接口只返回近约 20 个交易日，采用「入库累积」——
  每次同步把返回窗口 upsert 到 index_valuation_history（官方值，不掺入推导值）。
- `derive_index_dividend_yield`：用「全收益/价格指数比」推导**历史**股息率
  （近似口径），**仅在内存中计算、不落库**，供因子重算时叠加官方近期值使用。
"""

from __future__ import annotations

from datetime import date, timedelta
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


def merge_dividend_yield_history(derived_df: pd.DataFrame, official_df: pd.DataFrame) -> pd.DataFrame:
    """合并「推导全历史 + 官方近期」，官方值优先。

    返回 (trade_date / dividend_yield1)。官方值只覆盖其已有日期，其余日期用
    推导值补齐。两表任意为空时都可用另一张。
    """
    frames: list[pd.DataFrame] = []
    if derived_df is not None and not derived_df.empty:
        derived = derived_df[["trade_date", "dividend_yield1"]].copy()
        derived["source"] = DERIVED_SOURCE
        frames.append(derived)
    if official_df is not None and not official_df.empty:
        official = official_df[["trade_date", "dividend_yield1"]].copy()
        official["source"] = SOURCE
        frames.append(official)

    if not frames:
        return pd.DataFrame(columns=["trade_date", "dividend_yield1"])

    combined = pd.concat(frames, ignore_index=True)
    combined["trade_date"] = pd.to_datetime(combined["trade_date"], errors="coerce")
    combined["dividend_yield1"] = pd.to_numeric(combined["dividend_yield1"], errors="coerce")

    # 同一日期去重：官方（csindex）优先于推导（derived）
    source_priority = {DERIVED_SOURCE: 1, SOURCE: 2}
    combined["_src_rank"] = combined["source"].map(source_priority).fillna(0)
    combined = (
        combined.sort_values(["_src_rank"], ascending=False)
        .drop_duplicates(subset=["trade_date"], keep="first")
        .drop(columns=["_src_rank"])
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    return combined[["trade_date", "dividend_yield1"]]


def fetch_index_valuations(index_code: str) -> pd.DataFrame:
    """抓取指定指数的近期估值（PE1/PE2/股息率1/股息率2）。

    :param index_code: 指数代码，如 H30269。
    :return: DataFrame，列为 index_code / trade_date / pe1 / pe2 / dividend_yield1 /
        dividend_yield2 / source。
    """
    raw_df = ak.stock_zh_index_value_csindex(symbol=index_code)
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(
            columns=["index_code", "trade_date", "pe1", "pe2", "dividend_yield1", "dividend_yield2", "source"]
        )

    # 列名：日期 / 指数代码 / 中文全称 / 简称 / 英文全称 / 简称 / 市盈率1 / 市盈率2 / 股息率1 / 股息率2
    val_df = raw_df[["日期", "市盈率1", "市盈率2", "股息率1", "股息率2"]].copy()
    val_df.columns = ["trade_date", "pe1", "pe2", "dividend_yield1", "dividend_yield2"]

    val_df["index_code"] = str(index_code).strip()
    val_df["source"] = SOURCE
    val_df["trade_date"] = pd.to_datetime(val_df["trade_date"], errors="coerce")
    for column in ["pe1", "pe2", "dividend_yield1", "dividend_yield2"]:
        val_df[column] = pd.to_numeric(val_df[column], errors="coerce")

    val_df = val_df.dropna(subset=["trade_date", "dividend_yield1"])
    val_df = (
        val_df.drop_duplicates(subset=["index_code", "trade_date"], keep="last")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    return val_df[["index_code", "trade_date", "pe1", "pe2", "dividend_yield1", "dividend_yield2", "source"]]


def fetch_index_price(index_code: str) -> pd.DataFrame:
    """抓取指数价格收盘历史（全历史，csindex 官方行情）。

    策略口径 C（gemini 文档）：波动率 / 最大回撤取【指数价格】，而非基金净值。
    返回 trade_date / close（升序）。
    """
    raw_df = ak.stock_zh_index_hist_csindex(
        symbol=index_code,
        start_date="20000101",
        end_date=date.today().strftime("%Y%m%d"),
    )
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=["trade_date", "close"])

    price_df = raw_df[["日期", "收盘"]].rename(columns={"日期": "trade_date", "收盘": "close"}).copy()
    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"], errors="coerce")
    price_df["close"] = pd.to_numeric(price_df["close"], errors="coerce")
    price_df = (
        price_df.dropna(subset=["trade_date", "close"])
        .drop_duplicates(subset=["trade_date"], keep="last")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    return price_df[["trade_date", "close"]]
