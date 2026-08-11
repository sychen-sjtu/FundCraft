"""渐进式基金评估编排：数据齐到哪一层，就出到哪一层。

放置于 src/indicators/（指标计算层），遵循本项目分层约定：
- 数据读取统一走 src/fetchers / src/storage / src/config 的封装（不在此直接抓取）；
- 本模块只负责「编排」：逐层检查数据就绪度，产出可展示/可入库的结果。

分层（与 APP 三层架构一致）：
- Layer1 基金层：基金净值 / 分红 → 涨跌曲线、区间收益、最大回撤
- Layer2 指数层：指数价格 → 走势、收益、波动率、最大回撤
- Layer3 策略层：指数价格 + 股息率(全历史) + PE(全历史) + cn_10y → B 得分 / 信号 / 绩效

数据纪律（硬性要求）：
- 所有数据必须是【真实】数据，绝不模拟 / 估算；拿不到就返回 None，界面显示「暂无数据」。
- 数据读取接口统一走 Supabase 真实数据：
  · 基金净值 / 分红 / cn_10y → src/storage/supabase_store（需传入已连接的 client）
  · 基金→指数映射 → src/config.load_fund_index_codes
  · 指数价格 → index_daily_history.close；指数股息率 / PE → index_valuation_history
  任何一步取不到（未连接 / 库内无数据 / 异常）都返回 None，绝不降级为模拟值。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

# ============================================================================
# 一、数据读取接口（真实数据源）
#   约束：取不到 → 返回 None；成功 → 返回 DataFrame / 标量。
#   · 依赖 Supabase client 的：需传入已连接 client（由 UI 层 store 提供）
# ============================================================================


def _load_fund_nav(fund_code: str, client: Any) -> Any:
    """基金单位净值全历史（真实：Supabase fund_nav_history）。

    成功返回 DataFrame(fund_code, nav_date, unit_nav, daily_return)，否则 None。
    """
    if client is None:
        return None
    try:
        from src.storage.supabase_store import fetch_nav_history

        df = fetch_nav_history(client, fund_code)
        return df if not df.empty else None
    except Exception:  # noqa: BLE001 - 数据纪律：任何异常 → None
        return None


def _load_fund_dividends(fund_code: str, client: Any) -> Any:
    """基金分红记录（真实：Supabase fund_dividends）。

    成功返回 DataFrame(fund_code, ex_date, dividend_per_unit)，否则 None。
    """
    if client is None:
        return None
    try:
        from src.storage.supabase_store import (
            fetch_fund_dividends as fetch_fund_dividends_db,
        )

        df = fetch_fund_dividends_db(client, fund_code)
        return df if not df.empty else None
    except Exception:  # noqa: BLE001
        return None


def _load_fund_index_map(fund_code: str) -> str | None:
    """基金 → 跟踪指数代码（来自配置 [funds.categories.*].index_codes）。

    注意：008163 真实跟踪标普指数，当前配置为 H30269 占位（S&P 数据源未接入）；
    007466 跟踪中证红利低波，映射正确。未配置 / 未知返回 None。
    """
    try:
        from src.config import load_fund_index_codes

        return load_fund_index_codes().get(str(fund_code).strip())
    except Exception:  # noqa: BLE001
        return None


def _load_index_price(index_code: str, client: Any) -> Any:
    """指数收盘点位全历史（真实：Supabase index_daily_history.close）。

    成功返回 DataFrame(trade_date, close)，否则 None。
    """
    if client is None:
        return None
    try:
        from src.storage.supabase_store import _fetch_all_rows

        data = _fetch_all_rows(
            client.table("index_daily_history")
            .select("trade_date, close")
            .eq("index_code", index_code)
            .order("trade_date")
        )
        df = pd.DataFrame(data) if data else pd.DataFrame()
        if df.empty:
            return None
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df.dropna(subset=["trade_date", "close"])[["trade_date", "close"]].reset_index(drop=True)
    except Exception:  # noqa: BLE001 - 数据纪律：任何异常 → None
        return None


def _load_index_dividend_yield(index_code: str, client: Any) -> Any:
    """指数股息率全历史（%，真实：Supabase index_valuation_history.dividend_yield）。

    成功返回 DataFrame(trade_date, dividend_yield)，否则 None。
    """
    if client is None:
        return None
    try:
        from src.storage.supabase_store import _fetch_all_rows

        data = _fetch_all_rows(
            client.table("index_valuation_history")
            .select("trade_date, dividend_yield")
            .eq("index_code", index_code)
            .order("trade_date")
        )
        df = pd.DataFrame(data) if data else pd.DataFrame()
        if df.empty:
            return None
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df["dividend_yield"] = pd.to_numeric(df["dividend_yield"], errors="coerce")
        return df.dropna(subset=["trade_date", "dividend_yield"])[["trade_date", "dividend_yield"]].reset_index(drop=True)
    except Exception:  # noqa: BLE001
        return None


def _load_index_pe(index_code: str, client: Any) -> Any:
    """指数 PE 历史（真实：Supabase index_valuation_history.pe_ttm）。

    成功返回 DataFrame(trade_date, pe)，否则 None。
    """
    if client is None:
        return None
    try:
        from src.storage.supabase_store import _fetch_all_rows

        data = _fetch_all_rows(
            client.table("index_valuation_history")
            .select("trade_date, pe_ttm")
            .eq("index_code", index_code)
            .order("trade_date")
        )
        df = pd.DataFrame(data) if data else pd.DataFrame()
        if df.empty:
            return None
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df["pe"] = pd.to_numeric(df["pe_ttm"], errors="coerce")
        return df.dropna(subset=["trade_date", "pe"])[["trade_date", "pe"]].reset_index(drop=True)
    except Exception:  # noqa: BLE001
        return None


def _load_cn10y(client: Any) -> Any:
    """cn_10y 中国 10 年国债收益率全历史（真实：Supabase macro_rates_history）。

    成功返回 DataFrame(rate_code, rate_date, rate_value)，否则 None。
    """
    if client is None:
        return None
    try:
        from src.storage.supabase_store import fetch_macro_rates

        df = fetch_macro_rates(client, "cn_10y")
        return df if not df.empty else None
    except Exception:  # noqa: BLE001
        return None


# ============================================================================
# 二、各层计算（基于真实数据汇总就绪度）
# ============================================================================


def _build_fund_layer(nav: Any, dividends: Any) -> dict:
    """Layer1 基金层：净值 / 分红真实就绪度。"""
    nav_sorted = nav.sort_values("nav_date")
    return {
        "available": True,
        "nav_rows": int(len(nav)),
        "dividend_rows": int(len(dividends)) if dividends is not None else 0,
        "start_date": nav_sorted["nav_date"].iloc[0].strftime("%Y-%m-%d"),
        "end_date": nav_sorted["nav_date"].iloc[-1].strftime("%Y-%m-%d"),
    }


def _build_index_layer(index_price: Any) -> dict:
    """Layer2 指数层：指数行情真实就绪度。"""
    price_sorted = index_price.sort_values("trade_date")
    return {
        "available": True,
        "price_rows": int(len(index_price)),
        "start_date": price_sorted["trade_date"].iloc[0].strftime("%Y-%m-%d"),
        "end_date": price_sorted["trade_date"].iloc[-1].strftime("%Y-%m-%d"),
    }


def _build_strategy_layer(index_price: Any, dy: Any, pe: Any, rate: Any) -> dict:
    """Layer3 策略层：策略所需四类底层数据真实就绪度。"""
    return {
        "available": True,
        "price_rows": int(len(index_price)),
        "dy_rows": int(len(dy)) if dy is not None else 0,
        "pe_rows": int(len(pe)) if pe is not None else 0,
        "rate_rows": int(len(rate)) if rate is not None else 0,
        # 实际得分由 strategy_factors.compute_index_factors 计算（后续接入）
        "score_b": None,
        "signal_b": None,
    }


# ============================================================================
# 三、渐进式评估入口
# ============================================================================


def evaluate_fund(fund_code: str, *, client: Any = None) -> dict:
    """对单只基金做渐进式评估：数据齐到哪层就出到哪层，并说明缺什么。

    :param client: 已连接的 Supabase client（由 UI 层 store 传入）；未连接传 None
        → 依赖库读取的层（基金净值/分红/cn_10y）报「暂无数据」，绝不模拟。
    返回结构：
    {
      "fund_code": "007466",
      "status": "ok" | "partial" | "blocked",   # 就绪度
      "missing": ["fund_nav", ...],              # 缺失的数据项
      "hint": "……",                              # 面向用户的可读提示
      "layers": {
         "fund": {...},      # Layer1 结果（有数据才有）
         "index": {...},     # Layer2 结果（有数据才有）
         "strategy": {...},  # Layer3 结果（有数据才有）
      },
    }
    """
    result: dict = {
        "fund_code": str(fund_code),
        "status": "blocked",
        "missing": [],
        "hint": "暂无数据（未连接 Supabase，且无本地真实数据源）。",
        "layers": {},
    }

    # ---------- Layer 1：基金层（依赖 client） ----------
    nav = _load_fund_nav(fund_code, client)
    dividends = _load_fund_dividends(fund_code, client)
    if nav is None:
        result["missing"].append("fund_nav")
        result["hint"] = "暂无基金净值数据（未连接或库内无该基金净值）。"
        return result
    result["layers"]["fund"] = _build_fund_layer(nav, dividends)

    # ---------- 基金 → 指数映射 ----------
    index_code = _load_fund_index_map(fund_code)
    if index_code is None:
        result["missing"].append("fund_index_map")
        result["status"] = "partial"
        result["hint"] = "基金数据就绪，但未配置该基金对应的指数。"
        return result

    # ---------- Layer 2：指数层（Supabase index_daily_history） ----------
    index_price = _load_index_price(index_code, client)
    if index_price is None:
        result["missing"].append("index_price")
        result["status"] = "partial"
        result["hint"] = "指数数据未接入（库内无该指数行情 index_daily_history），暂无指数行情。"
        return result
    result["layers"]["index"] = _build_index_layer(index_price)

    # ---------- Layer 3：策略层 ----------
    dy = _load_index_dividend_yield(index_code, client)
    pe = _load_index_pe(index_code, client)
    rate = _load_cn10y(client)
    missing = [
        name
        for name, value in {
            "index_dividend_yield": dy,
            "index_pe": pe,
            "cn10y": rate,
        }.items()
        if value is None
    ]
    if missing:
        result["missing"].extend(missing)
        result["status"] = "partial"
        result["hint"] = f"指数行情就绪，策略层缺数据：{', '.join(missing)}。"
        return result
    result["layers"]["strategy"] = _build_strategy_layer(index_price, dy, pe, rate)

    result["status"] = "ok"
    result["hint"] = "评估完成：基金 / 指数 / 策略三层数据均已就绪。"
    return result


if __name__ == "__main__":
    # 冒烟测试：
    #   - 不传 client（未连接）：基金层应「暂无数据 / blocked」
    #   - 传已连接 client：基金层就绪；指数/策略层来自真实理杏仁 CSV
    import json

    for code in ("007466", "008163"):
        print(json.dumps(evaluate_fund(code), ensure_ascii=False, indent=2))
