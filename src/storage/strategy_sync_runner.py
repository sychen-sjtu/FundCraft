"""正式策略数据同步任务：按 watermark 判断「全量初始化 / 增量补全」，并重算派生因子。

流程（见 docs/数据持久化与增量同步设计方案.md）：
1. 对每个基金：读水位 → 无水位则全量初始化，有水位则从 last_date - 重叠窗口 增量补全
   （净值 + 分红 + 基础信息），然后更新水位。
2. 对 cn_10y 利率：同上处理。
3. 从持久化的净值 + 分红 + 利率重算派生因子（fund_daily_factors）并入库。

触发方式：
- UI 侧边栏「刷新数据」按钮（调用 refresh_with_client）
- 命令行 python -m src.storage.strategy_sync_runner [--entity fund|rate] [--code xxx] [--mode init|incremental]
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.config import (
    load_factor_fund_codes,
    load_fund_codes,
    load_fund_index_codes,
    load_supabase_settings,
    supabase_settings_ready,
)
from src.fetchers.akshare_fund_nav import (
    fetch_fund_nav_history,
    fetch_fund_profiles,
    normalize_fund_code,
)
# 注意：AkShare 抓取版与 Supabase 读取版同名，必须用别名区分，否则会发生覆盖
from src.fetchers.fund_dividend_fetcher import fetch_fund_dividends as fetch_fund_dividends_ak
from src.fetchers.index_valuation_fetcher import (
    derive_index_dividend_yield,
    fetch_index_valuations as fetch_index_valuations_ak,
    merge_dividend_yield_history,
)
from src.fetchers.macro_fetcher import fetch_cn_10y_rate
from src.indicators.strategy_factors import compute_fund_factors
from src.storage.supabase_store import (
    create_supabase_client,
    delete_fund_daily_factors,
    fetch_fund_dividends as fetch_fund_dividends_db,
    fetch_index_valuations as fetch_index_valuations_db,
    fetch_macro_rates,
    fetch_nav_history,
    get_watermark,
    insert_sync_log,
    upsert_daily_factors,
    upsert_fund_dividends,
    upsert_fund_profiles,
    upsert_index_valuations,
    upsert_macro_rates,
    upsert_nav_history,
    upsert_watermark,
)

# 增量补全的重叠窗口：从 last_date 往回多拉 N 天，兜底上游净值修正/订正
OVERLAP_DAYS = 10


def _trim_to_increment(df: pd.DataFrame, date_column: str, last_date) -> pd.DataFrame:
    """按水位截取增量范围（last_date - overlap 之后的行）。"""
    cutoff = pd.Timestamp(last_date) - pd.Timedelta(days=OVERLAP_DAYS)
    return df[df[date_column] >= cutoff].copy()


def sync_single_fund(client, fund_code: str, *, mode: str | None = None) -> dict:
    """同步单只基金（净值 + 分红 + 基础信息），返回汇总信息。"""
    fund_code = normalize_fund_code(fund_code)
    watermark_df = get_watermark(client, "fund", fund_code)
    has_watermark = not watermark_df.empty
    if mode is None:
        mode = "incremental" if has_watermark else "init"

    # 净值：AkShare 只返回全历史，按模式决定入库范围（增量时只补缺失部分）
    nav_df = fetch_fund_nav_history(fund_code)
    if nav_df.empty:
        raise ValueError(f"No NAV data for {fund_code}")

    if mode == "incremental" and has_watermark:
        last_date = watermark_df["last_date"].iloc[0]
        upsert_df = _trim_to_increment(nav_df, "nav_date", last_date)
    else:
        upsert_df = nav_df
    upsert_nav_history(client, upsert_df)

    # 分红：全量抓取（数据量小、幂等）
    dividend_df = fetch_fund_dividends_ak([fund_code])
    dividend_count = upsert_fund_dividends(client, dividend_df)

    # 基础信息（名称/类型/跟踪指数）
    profiles = fetch_fund_profiles([fund_code])
    upsert_fund_profiles(client, profiles)

    # 更新水位
    new_last_date = nav_df["nav_date"].max()
    upsert_watermark(client, "fund", fund_code, new_last_date, source="fund_open_fund_info_em")

    return {
        "entity": "fund",
        "fund_code": fund_code,
        "mode": mode,
        "nav_rows": int(len(upsert_df)),
        "dividend_rows": dividend_count,
        "last_date": str(new_last_date.date()),
    }


def sync_rate(client, rate_code: str = "cn_10y", *, mode: str | None = None) -> dict:
    """同步宏观利率（cn_10y）。"""
    watermark_df = get_watermark(client, "rate", rate_code)
    has_watermark = not watermark_df.empty
    if mode is None:
        mode = "incremental" if has_watermark else "init"

    rate_df = fetch_cn_10y_rate()
    if rate_df.empty:
        raise ValueError(f"No rate data for {rate_code}")

    if mode == "incremental" and has_watermark:
        last_date = watermark_df["last_date"].iloc[0]
        upsert_df = _trim_to_increment(rate_df, "rate_date", last_date)
    else:
        upsert_df = rate_df

    count = upsert_macro_rates(client, upsert_df)
    new_last_date = rate_df["rate_date"].max()
    upsert_watermark(client, "rate", rate_code, new_last_date, source="bond_zh_us_rate")

    return {
        "entity": "rate",
        "rate_code": rate_code,
        "mode": mode,
        "rows": count,
        "last_date": str(new_last_date.date()),
    }


def sync_index_valuation(client, index_code: str, *, mode: str | None = None) -> dict:
    """同步指数估值（csindex 近约 20 个交易日，入库累积成历史）。"""
    watermark_df = get_watermark(client, "index", index_code)
    has_watermark = not watermark_df.empty
    if mode is None:
        mode = "incremental" if has_watermark else "init"

    valuation_df = fetch_index_valuations_ak(index_code)
    if valuation_df.empty:
        raise ValueError(f"No valuation data for index {index_code}")

    count = upsert_index_valuations(client, valuation_df)
    new_last_date = valuation_df["trade_date"].max()
    upsert_watermark(client, "index", index_code, new_last_date, source="stock_zh_index_value_csindex")

    return {
        "entity": "index",
        "index_code": index_code,
        "mode": mode,
        "rows": count,
        "last_date": str(new_last_date.date()),
    }


def recompute_factors(client, fund_code: str, *, index_code: str | None = None) -> dict:
    """从持久化的净值 + 「推导历史 + 官方近期」股息率 + 利率重算派生因子并入库。

    股息率口径（A2 混合，不污染数据库）：
    - 官方指数股息率仍走 index_valuation_history 入库累积（源=csindex）；
    - 推导历史股息率（全收益/价格比）只在内存计算，用于补齐官方缺失的历史，
      不落库；两者合并时官方值优先。
    """
    fund_code = normalize_fund_code(fund_code)
    nav_df = fetch_nav_history(client, fund_code)
    if nav_df.empty:
        return {"entity": "fund", "fund_code": fund_code, "factor_rows": 0}

    if index_code is None:
        index_code = load_fund_index_codes().get(fund_code)

    if index_code:
        official_df = fetch_index_valuations_db(client, index_code)
        try:
            derived_df = derive_index_dividend_yield(index_code)
        except Exception as exc:  # noqa: BLE001 - 推导失败回退为仅官方
            print(f"WARN: derive_index_dividend_yield({index_code}) failed: {exc}")
            derived_df = pd.DataFrame()
        dividend_history = merge_dividend_yield_history(derived_df, official_df)
    else:
        dividend_history = pd.DataFrame()

    rate_df = fetch_macro_rates(client, "cn_10y")
    factors_df = compute_fund_factors(nav_df, dividend_history, rate_df)

    # 先清理旧因子（避免旧口径残留污染），再入库
    delete_fund_daily_factors(client, fund_code)
    count = upsert_daily_factors(client, factors_df)
    return {"entity": "fund", "fund_code": fund_code, "factor_rows": count}


def refresh_with_client(client, fund_codes, *, factor_fund_codes: list[str] | None = None) -> list[dict]:
    """使用已连接的 Supabase client 执行一次完整刷新（UI 与 CLI 共用）。

    顺序很重要：
    1. 先同步所有基金的原始数据（净值 + 分红 + 基础信息）；
    2. 再同步 cn_10y 利率；
    3. 再同步各基金对应指数的估值（股息率，入库累积）；
    4. 最后统一重算派生因子（此时 净值/利率/指数估值 都已入库）。

    :param factor_fund_codes: 参与因子计算的基金（由类别面板派生）；为 None 时
        按 load_factor_fund_codes() 从类别配置推导（panel ∈ FACTOR_PANELS）。
    """
    if factor_fund_codes is None:
        factor_fund_codes = [normalize_fund_code(code) for code in load_factor_fund_codes()]

    results: list[dict] = []
    total_rows = 0

    # 1) 同步基金原始数据
    for code in fund_codes:
        normalized = normalize_fund_code(code)
        try:
            fund_result = sync_single_fund(client, normalized)
            results.append(fund_result)
            total_rows += int(fund_result["nav_rows"])
        except Exception as exc:  # noqa: BLE001 - 单只失败不阻塞其它基金
            results.append({"entity": "fund", "fund_code": normalized, "error": str(exc)})

    # 2) 同步利率
    try:
        results.append(sync_rate(client, "cn_10y"))
    except Exception as exc:  # noqa: BLE001
        results.append({"entity": "rate", "rate_code": "cn_10y", "error": str(exc)})

    # 3) 同步指数估值（入库累积）——收集所有基金配置的指数代码
    fund_index_map = load_fund_index_codes()
    for index_code in sorted({ic for ic in fund_index_map.values() if ic}):
        try:
            results.append(sync_index_valuation(client, index_code))
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "index", "index_code": index_code, "error": str(exc)})

    # 4) 统一重算因子（仅策略基金参与；债基现金池只存净值、不计算策略因子）
    for code in factor_fund_codes:
        normalized = normalize_fund_code(code)
        try:
            factor_result = recompute_factors(client, normalized, index_code=fund_index_map.get(normalized))
            for existing in results:
                if existing.get("entity") == "fund" and existing.get("fund_code") == normalized:
                    existing.update(factor_result)
                    break
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "fund", "fund_code": normalized, "factor_error": str(exc)})

    insert_sync_log(
        client,
        job_name="strategy_refresh",
        status="success" if not any("error" in r or "factor_error" in r for r in results) else "partial",
        message=f"Refreshed {len(fund_codes)} funds + cn_10y",
        row_count=total_rows,
    )

    for result in results:
        print(result)
    return results


def run_strategy_refresh(project_root: Path | None = None, *, secret_password: str | None = None, fund_codes=None) -> list[dict]:
    """命令行入口：加载配置 → 连接 Supabase → 完整刷新。"""
    root = project_root or Path(__file__).resolve().parents[2]
    settings = load_supabase_settings(root, secret_password=secret_password)
    if not supabase_settings_ready(settings):
        raise ValueError("Supabase settings are missing. Fill .streamlit/secrets.toml first.")

    client = create_supabase_client(settings)
    fund_codes = fund_codes or load_fund_codes(root)
    if not fund_codes:
        raise ValueError("未在 .streamlit/secrets.toml 中配置基金代码（[funds] fund_codes）。")

    factor_fund_codes = load_factor_fund_codes(root)
    return refresh_with_client(client, fund_codes, factor_fund_codes=factor_fund_codes)


def _run_cli() -> None:
    parser = argparse.ArgumentParser(description="FundCraft 策略数据同步（初始化/增量 + 因子重算）")
    parser.add_argument("--entity", choices=["fund", "rate", "index", "all"], default="all", help="同步实体类型")
    parser.add_argument("--code", default=None, help="实体代码（基金代码 / cn_10y / 指数代码）")
    parser.add_argument("--mode", choices=["init", "incremental"], default=None, help="强制指定模式；缺省按水位自动判断")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    settings = load_supabase_settings(root)
    if not supabase_settings_ready(settings):
        raise SystemExit("Supabase settings are missing. Fill .streamlit/secrets.toml first.")
    client = create_supabase_client(settings)

    results: list[dict] = []
    fund_index_map = load_fund_index_codes(root)

    if args.entity in ("fund", "all"):
        fund_codes = [normalize_fund_code(args.code)] if args.code else load_fund_codes(root)
        # 因子重算范围：显式 --code 时对该基金重算；否则只对策略基金重算
        factor_codes = [normalize_fund_code(args.code)] if args.code else [normalize_fund_code(c) for c in load_factor_fund_codes(root)]
        for code in fund_codes:
            try:
                fund_result = sync_single_fund(client, code, mode=args.mode)
                if normalize_fund_code(code) in factor_codes:
                    factor_result = recompute_factors(client, code, index_code=fund_index_map.get(normalize_fund_code(code)))
                    fund_result.update(factor_result)
                results.append(fund_result)
            except Exception as exc:  # noqa: BLE001
                results.append({"entity": "fund", "fund_code": code, "error": str(exc)})

    if args.entity in ("rate", "all"):
        rate_code = args.code or "cn_10y"
        try:
            results.append(sync_rate(client, rate_code, mode=args.mode))
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "rate", "rate_code": rate_code, "error": str(exc)})

    # 指数估值同步（入库累积）：entity=all 或显式传入指数代码时
    index_codes_to_sync: list[str] = []
    if args.entity in ("index", "all"):
        index_codes_to_sync = [args.code] if args.code else sorted({ic for ic in fund_index_map.values() if ic})
    for index_code in index_codes_to_sync:
        try:
            results.append(sync_index_valuation(client, index_code, mode=args.mode))
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "index", "index_code": index_code, "error": str(exc)})

    insert_sync_log(
        client,
        job_name="strategy_sync_cli",
        status="success" if not any("error" in r for r in results) else "partial",
        message=f"CLI sync: {args.entity}",
        row_count=0,
    )
    for result in results:
        print(result)


if __name__ == "__main__":
    _run_cli()
