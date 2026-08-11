"""数据同步编排：配置(TOML→表) + 基金/利率/指数/因子 各层刷新 + 完整刷新。

新 ER 结构（UI 与 CLI 共用同一套逻辑）：
- refresh_all：全量刷新（配置 → 基金 → cn_10y → 指数行情 → 指数估值 → 策略因子）
- refresh_layer：按层刷新（fund / index / rate / factors），写 sync_watermark + sync_job
- sync_config：TOML → index_master / fund_tracking_index

触发方式：
- UI 数据管理页「增量/强制全量刷新」（refresh_all）与「按层刷新」（refresh_layer）
- 命令行 python -m src.storage.strategy_sync_runner [--entity all|config|fund|rate|index|factors]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.config import (
    IndexSpec,
    load_fund_codes,
    load_fund_tracking_index,
    load_index_registry,
    load_supabase_settings,
    supabase_settings_ready,
)
from src.fetchers.akshare_fund_nav import (
    fetch_fund_nav_history,
    fetch_fund_profiles,
    normalize_fund_code,
)
from src.fetchers.fund_dividend_fetcher import fetch_fund_dividends as fetch_fund_dividends_ak
from src.fetchers.index_valuation_fetcher import derive_index_dividend_yield
from src.fetchers.macro_fetcher import fetch_cn_10y_rate
from src.storage.supabase_store import (
    create_supabase_client,
    delete_stale_fund_tracking_index,
    delete_stale_index_master,
    insert_sync_log,
    upsert_fund_dividends,
    upsert_fund_profiles,
    upsert_fund_tracking_index,
    upsert_index_master,
    upsert_index_valuations,
    upsert_macro_rates,
    upsert_nav_history,
    upsert_watermark,
)

def sync_config(client) -> dict:
    """从 TOML 配置同步「指数注册表 + 基金→指数映射」到库。

    配置源（唯一，调整后重跑同步即生效，不在 SQL/代码中硬编码）：
    - 指数注册表  → .streamlit/secrets.toml 的 [indexes.registry]
    - 基金→指数   → [funds.categories.*].index_codes（role='strategy'）
    兜底：被基金映射引用但未登记的指数自动补登记到 index_master（避免外键失败）。
    对账：删除配置中已移除的映射/指数（调整 TOML 后重跑即生效，含删除）。
    """
    registry = load_index_registry()
    tracking = load_fund_tracking_index()

    specs: dict[str, IndexSpec] = dict(registry)
    tracking_codes: set[str] = set()
    for fund_code, index_code, role in tracking:
        tracking_codes.add(index_code)
        if index_code not in specs:
            specs[index_code] = IndexSpec(index_code=index_code, index_category="strategy", source="csindex")

    index_rows = upsert_index_master(client, specs.values())
    mapping_rows = upsert_fund_tracking_index(client, tracking)
    stale_mapping = delete_stale_fund_tracking_index(client, [(f, ic) for f, ic, _ in tracking])
    stale_index = delete_stale_index_master(client, list(specs.keys()), referenced=tracking_codes)
    return {
        "entity": "config",
        "index_rows": index_rows,
        "mapping_rows": mapping_rows,
        "stale_mapping_deleted": stale_mapping,
        "stale_index_deleted": stale_index,
    }


def _refresh_funds(client, fund_codes: list[str]) -> list[dict]:
    """基金层：净值(复权) + 分红 + 档案 + 水位。"""
    from src.fetchers.akshare_fund_nav import derive_adjusted_nav

    results: list[dict] = []
    for code in fund_codes:
        try:
            nav = derive_adjusted_nav(fetch_fund_nav_history(code))
            upsert_nav_history(client, nav)
            div = fetch_fund_dividends_ak([code])
            upsert_fund_dividends(client, div)
            upsert_fund_profiles(client, fetch_fund_profiles([code]))
            if not nav.empty and "nav_date" in nav.columns:
                upsert_watermark(client, "fund", code, nav["nav_date"].max(), source="fund_open_fund_info_em")
            results.append({"entity": "fund", "fund_code": code, "nav_rows": len(nav), "dividend_rows": len(div)})
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "fund", "fund_code": code, "error": str(exc)})
    return results


def _refresh_rate(client) -> list[dict]:
    """宏观层：cn_10y + 水位。"""
    try:
        rate = fetch_cn_10y_rate()
        n = upsert_macro_rates(client, rate)
        if not rate.empty and "rate_date" in rate.columns:
            upsert_watermark(client, "rate", "cn_10y", rate["rate_date"].max(), source="bond_zh_us_rate")
        return [{"entity": "rate", "rows": n}]
    except Exception as exc:  # noqa: BLE001
        return [{"entity": "rate", "error": str(exc)}]


def _refresh_indexes(client, registry) -> list[dict]:
    """指数层-行情：价格/全收益日行情 + 水位（000300S 用 H00300 拉取）。"""
    from src.fetchers.index_valuation_fetcher import fetch_index_daily_history
    from src.storage.supabase_store import upsert_index_daily_history

    symbol_map = {"000300S": "H00300"}
    results: list[dict] = []
    for index_code in sorted(registry.keys()):
        try:
            df = fetch_index_daily_history(symbol_map.get(index_code, index_code))
            if not df.empty:
                df["index_code"] = index_code
                n = upsert_index_daily_history(client, df)
                if "trade_date" in df.columns:
                    upsert_watermark(client, "index", index_code, df["trade_date"].max(), source="stock_zh_index_value_csindex")
                results.append({"entity": "index", "index_code": index_code, "daily_rows": n})
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "index", "index_code": index_code, "error": str(exc)})
    return results


def _refresh_valuation(client, index_codes: tuple[str, ...] = ("H30269", "000300")) -> list[dict]:
    """指数层-估值：官方近20日累积 + 推导历史前缀（source 标注）。"""
    import akshare as _ak

    from src.fetchers.index_valuation_fetcher import derive_index_dividend_yield

    results: list[dict] = []
    for index_code in index_codes:
        try:
            official_raw = _ak.stock_zh_index_value_csindex(symbol=index_code)
            if official_raw is not None and not official_raw.empty:
                official = official_raw[["日期", "市盈率1", "市盈率2", "股息率1"]].rename(
                    columns={"日期": "trade_date", "市盈率1": "pe_ttm", "市盈率2": "pe_lyr", "股息率1": "dividend_yield"}
                )
                official["index_code"] = index_code
                official["trade_date"] = pd.to_datetime(official["trade_date"], errors="coerce")
                for col in ("pe_ttm", "pe_lyr", "dividend_yield"):
                    official[col] = pd.to_numeric(official[col], errors="coerce")
                official = official.dropna(subset=["trade_date", "dividend_yield"])
                upsert_index_valuations(client, official, source="csindex")
                results.append({"entity": "valuation", "index_code": index_code, "official_rows": len(official)})

                # 推导前缀（只补官方之前，source='derived'）
                if index_code == "H30269":
                    derived = derive_index_dividend_yield(index_code)
                    if not derived.empty:
                        prefix = derived[derived["trade_date"] < official["trade_date"].min()].copy()
                        prefix = prefix.rename(columns={"dividend_yield1": "dividend_yield"})
                        prefix["index_code"] = index_code
                        n_derived = upsert_index_valuations(client, prefix, source="derived")
                        results.append({"entity": "valuation", "index_code": index_code, "derived_rows": n_derived})
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "valuation", "index_code": index_code, "error": str(exc)})
    return results


def _refresh_factors(client) -> list[dict]:
    """策略层：指数日频因子重算（波动/回撤取全收益 H20269）。"""
    from src.indicators.strategy_factors import compute_index_factors
    from src.storage.supabase_store import (
        _fetch_all_rows,
        delete_index_daily_factors,
        upsert_index_daily_factors,
    )

    try:
        dy_df = pd.DataFrame(
            _fetch_all_rows(client.table("index_valuation_history").select("trade_date,dividend_yield").eq("index_code", "H30269"))
        )
        price_df = pd.DataFrame(
            _fetch_all_rows(client.table("index_daily_history").select("trade_date,close").eq("index_code", "H20269"))
        )
        rate_df = pd.DataFrame(
            _fetch_all_rows(client.table("macro_rates_history").select("trade_date,rate_value").eq("rate_code", "cn_10y"))
        )
        for df in (dy_df, price_df, rate_df):
            if not df.empty and "trade_date" in df.columns:
                df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        if dy_df.empty or price_df.empty or rate_df.empty:
            raise ValueError(f"因子输入不足：股息率={len(dy_df)} 行情={len(price_df)} 利率={len(rate_df)}")
        factors = compute_index_factors("H30269", dy_df, price_df, rate_df)
        if not factors.empty:
            delete_index_daily_factors(client, "H30269")
            n = upsert_index_daily_factors(client, factors)
            return [{"entity": "factors", "index_code": "H30269", "factor_rows": n}]
        return [{"entity": "factors", "index_code": "H30269", "error": "因子计算为空"}]
    except Exception as exc:  # noqa: BLE001
        return [{"entity": "factors", "index_code": "H30269", "error": str(exc)}]


def refresh_all(client) -> list[dict]:
    """新 ER 结构完整刷新（UI「刷新数据」按钮用）。

    顺序：配置 → 基金(档案/净值/分红) → cn_10y → 指数行情(价格+全收益) → 指数估值(官方+推导) → 策略因子。
    """
    results: list[dict] = []
    try:
        results.append(sync_config(client))
    except Exception as exc:  # noqa: BLE001
        results.append({"entity": "config", "error": str(exc)})

    fund_codes = [normalize_fund_code(code) for code in load_fund_codes()]
    registry = load_index_registry()

    results += _refresh_funds(client, fund_codes)
    results += _refresh_rate(client)
    results += _refresh_indexes(client, registry)
    results += _refresh_valuation(client)
    results += _refresh_factors(client)

    insert_sync_log(
        client,
        job_name="refresh_all",
        status="success" if not any("error" in r for r in results) else "partial",
        message=f"Refreshed {len(fund_codes)} funds + index/rate/valuation/factors",
        row_count=0,
    )
    return results


LAYER_KEYS = ("fund", "index", "rate", "factors")


def refresh_layer(client, layer_key: str) -> tuple[list[dict], str | None]:
    """按层刷新（UI「按层刷新」按钮用），返回 (结果列表, 错误信息)。

    - fund → 基金净值/分红/档案（同步水位补齐）
    - rate → cn_10y 利率
    - index → 指数行情 + 估值（官方+推导）
    - factors → 策略因子重算
    """
    layer_key = (layer_key or "").lower()
    if layer_key == "fund":
        fund_codes = [normalize_fund_code(code) for code in load_fund_codes()]
        results = _refresh_funds(client, fund_codes)
    elif layer_key == "rate":
        results = _refresh_rate(client)
    elif layer_key == "index":
        registry = load_index_registry()
        results = _refresh_indexes(client, registry) + _refresh_valuation(client)
    elif layer_key == "factors":
        results = _refresh_factors(client)
    else:
        return [], f"未知数据层：{layer_key}"

    errors = [
        f"{r.get('entity')}:{r.get('index_code') or r.get('fund_code') or ''}:{r['error']}"
        for r in results
        if "error" in r
    ]
    error = "、".join(errors) or None
    insert_sync_log(
        client,
        job_name=f"layer_{layer_key}",
        status="error" if error else "success",
        message=error or f"Layer {layer_key} refreshed",
        row_count=len(results),
    )
    return results, error


def _run_cli() -> None:
    """CLI 入口：完整刷新（all）或按层刷新（config/fund/rate/index/factors）。

    与 UI 共用同一套编排（refresh_all / refresh_layer / sync_config），避免多套同步逻辑。
    """
    parser = argparse.ArgumentParser(description="FundCraft 数据同步（配置/基金/利率/指数/因子）")
    parser.add_argument(
        "--entity",
        choices=["all", "config", "fund", "rate", "index", "factors"],
        default="all",
        help="刷新范围：all 全量；config 配置；fund 基金；rate 利率；index 指数；factors 因子",
    )
    parser.add_argument("--code", default=None, help="基金代码（仅 --entity fund 时按单只刷新）")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    settings = load_supabase_settings(root)
    if not supabase_settings_ready(settings):
        raise SystemExit("Supabase settings are missing. Fill .streamlit/secrets.toml first.")
    client = create_supabase_client(settings)

    if args.entity == "all":
        results = refresh_all(client)
    elif args.entity == "config":
        results = [sync_config(client)]
    elif args.entity == "fund" and args.code:
        results = _refresh_funds(client, [normalize_fund_code(args.code)])
    else:
        results, _ = refresh_layer(client, args.entity)

    for result in results:
        print(result)


if __name__ == "__main__":
    _run_cli()
