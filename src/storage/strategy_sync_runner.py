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
from datetime import date, datetime, timedelta
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
    fetch_nav_history,
    insert_sync_log,
    list_fund_profiles,
    list_watermarks,
    upsert_fund_dividends,
    upsert_fund_profiles,
    upsert_fund_tracking_index,
    upsert_index_master,
    upsert_index_valuations,
    upsert_macro_rates,
    upsert_nav_history,
    upsert_watermark,
)

class SyncProgress:
    """轻量进度跟踪：不绑定 UI 框架，由调用方提供 reporter 回调。

    reporter(done, total, label) 由 UI（如 Streamlit 进度条）实现；
    CLI / 无 UI 调用时传 None 即静默。
    """

    __slots__ = ("total", "done", "_reporter")

    def __init__(self, total: int, reporter=None) -> None:
        self.total = max(total, 1)
        self.done = 0
        self._reporter = reporter

    def report(self, done: int, label: str) -> None:
        """绝对进度：直接把 done 设到指定值（用于层间跳转 / 锁定边界）。"""
        self.done = max(0, min(done, self.total))
        if self._reporter:
            self._reporter(self.done, self.total, label)

    def step(self, label: str, n: int = 1) -> None:
        """步进进度：完成 n 步并上报。"""
        self.done = min(self.done + n, self.total)
        if self._reporter:
            self._reporter(self.done, self.total, label)


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


# 增量拉取的日期余量（天）：从「水位最新日期 - 余量」起补拉，覆盖边界日与数据修正。
OVERLAP_DAYS = 10


def _watermark_map(client) -> dict[tuple[str, str], pd.Timestamp]:
    """读取同步水位：{(entity_type, entity_code): last_date}。"""
    wm = list_watermarks(client)
    result: dict[tuple[str, str], pd.Timestamp] = {}
    if wm.empty:
        return result
    for row in wm.itertuples(index=False):
        result[(str(row.entity_type), str(row.entity_code))] = pd.Timestamp(row.last_date)
    return result


def _since_date(wm: dict, key: tuple[str, str]) -> str | None:
    """增量拉取起点 = 水位 - OVERLAP_DAYS（ISO 日期）；无水位返回 None（全量拉取）。"""
    last = wm.get(key)
    if last is None:
        return None
    return (last - pd.Timedelta(days=OVERLAP_DAYS)).date().isoformat()


def _last_adjusted_nav(client, code: str, before: str) -> float | None:
    """读取指定日期之前最后一行已入库的复权净值，作为增量复权接续锚点（无则 None）。"""
    prev = fetch_nav_history(client, code, end_date=before)
    if prev.empty or "adjusted_nav" not in prev.columns:
        return None
    series = prev["adjusted_nav"].dropna()
    return float(series.iloc[-1]) if not series.empty else None


def _refresh_funds(client, fund_codes: list[str], progress: SyncProgress | None = None) -> list[dict]:
    """基金层：净值(复权) + 分红 + 档案 + 水位。

    增量策略（数据为空 → 全量；非空 → 至少拉「水位 - OVERLAP_DAYS」重叠窗口）：
    - 净值：无论水位新旧都至少拉 OVERLAP 窗口，兜底「净值数据修正」（避免完全跳过漏掉调整）；首次才全量。
    - 分红：全部基金合并一次抓取，至少扫「当前年」兜底分红修正；有水位回退 1 年，空则 2015 全量。
    - 档案：静态数据，档案表里已存在的基金不重复抓取（避免重复下载全市场列表 / 雪球逐只查询）。
    """
    from src.fetchers.akshare_fund_nav import derive_adjusted_nav

    results: list[dict] = []
    wm = _watermark_map(client)

    # ---- 净值（增量：有水位则按「水位-OVERLAP_DAYS」只拉增量窗口，网络不再全量） ----
    for code in fund_codes:
        if progress:
            progress.step(f"基金 {code}：净值 / 复权")
        try:
            last = wm.get(("fund", code))
            since = _since_date(wm, ("fund", code))
            if since is None:
                # 首次：akshare 全历史 + 从头推导复权
                nav = derive_adjusted_nav(fetch_fund_nav_history(code))
            else:
                # 增量：至少拉「水位-OVERLAP_DAYS」重叠窗口（兜底最近净值修正）+ 接续锚点复权
                before = (pd.Timestamp(since) - pd.Timedelta(days=1)).date().isoformat()
                anchor = _last_adjusted_nav(client, code, before=before)
                if anchor is None:
                    nav = derive_adjusted_nav(fetch_fund_nav_history(code))
                else:
                    try:
                        nav = derive_adjusted_nav(
                            fetch_fund_nav_history(code, start_date=since),
                            prev_adjusted_nav=anchor,
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"WARN: 增量拉取 {code} 失败，回退全量: {exc}")
                        nav = derive_adjusted_nav(fetch_fund_nav_history(code))

            if nav.empty:
                results.append({"entity": "fund", "fund_code": code, "nav_rows": 0, "note": "无新增净值"})
                continue
            n = upsert_nav_history(client, nav, since_date=since)
            # 水位只前进不倒退（若本次窗口未覆盖到原水位，保留原值，避免误回退）
            max_date = nav["nav_date"].max()
            if last is not None:
                max_date = max(pd.Timestamp(max_date), last)
            upsert_watermark(client, "fund", code, max_date, source="fund_open_fund_info_em")
            results.append({"entity": "fund", "fund_code": code, "nav_rows": n})
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "fund", "fund_code": code, "error": str(exc)})

    # ---- 分红（合并一次抓取；至少扫「当前年」兜底修正，有水位回退 1 年，空则 2015 全量） ----
    if progress:
        progress.step(f"分红：{len(fund_codes)} 只基金合并抓取")
    try:
        years = [wm.get(("fund", code)).year for code in fund_codes if wm.get(("fund", code)) is not None]
        all_current = bool(years) and all(
            wm.get(("fund", code)) is not None
            and wm[("fund", code)].date() >= date.today() - timedelta(days=1)
            for code in fund_codes
        )
        # 水位均最新 → 只扫当前年（轻量兜底分红修正）；否则按水位回退 1 年；无水位全量
        start_year = datetime.now().year if all_current else (max(2015, min(years) - 1) if years else 2015)
        div = fetch_fund_dividends_ak(fund_codes, start_year=start_year)
        n_div = upsert_fund_dividends(client, div)
        if n_div:
            results.append({"entity": "dividends", "fund_codes": list(fund_codes), "dividend_rows": n_div})
    except Exception as exc:  # noqa: BLE001
        results.append({"entity": "dividends", "error": str(exc)})

    # ---- 档案（仅缺档基金抓取；静态数据不重复拉） ----
    try:
        existing: set[str] = set()
        profiles_df = list_fund_profiles(client)
        if not profiles_df.empty and "fund_code" in profiles_df.columns:
            existing = {str(code) for code in profiles_df["fund_code"].dropna()}
        missing = [code for code in fund_codes if normalize_fund_code(code) not in existing]
        if missing:
            if progress:
                progress.step(f"档案：{len(missing)} 只基金补抓")
            upsert_fund_profiles(client, fetch_fund_profiles(missing))
        results.append({"entity": "profiles", "fetched": missing, "skipped": [c for c in fund_codes if c not in missing]})
    except Exception as exc:  # noqa: BLE001
        results.append({"entity": "profiles", "error": str(exc)})

    return results


def _refresh_rate(client, progress: SyncProgress | None = None) -> list[dict]:
    """宏观层：cn_10y + 国债期货(TF/T) + 水位（增量：从「水位 - OVERLAP_DAYS」起拉，空则全量）。"""
    from src.fetchers.bond_futures_fetcher import BOND_FUTURES, fetch_bond_futures_daily

    results: list[dict] = []
    if progress:
        progress.step("宏观层：cn_10y 利率")
    try:
        since = _since_date(_watermark_map(client), ("rate", "cn_10y"))
        start = since.replace("-", "") if since else "20000101"
        rate = fetch_cn_10y_rate(start_date=start)
        n = upsert_macro_rates(client, rate)
        if not rate.empty and "rate_date" in rate.columns:
            upsert_watermark(client, "rate", "cn_10y", rate["rate_date"].max(), source="bond_zh_us_rate")
        results.append({"entity": "rate", "rows": n})
    except Exception as exc:  # noqa: BLE001
        results.append({"entity": "rate", "error": str(exc)})

    # 国债期货主力连续日线（TF/T 各一个 rate_code，落 macro_rates_history）
    for symbol, rate_code, _name in BOND_FUTURES:
        if progress:
            progress.step(f"宏观层：国债期货 {symbol}")
        try:
            since = _since_date(_watermark_map(client), ("rate", rate_code))
            start = since.replace("-", "") if since else "20170101"
            futures = fetch_bond_futures_daily(symbol, start_date=start)
            n = upsert_macro_rates(client, futures)
            if not futures.empty and "trade_date" in futures.columns:
                upsert_watermark(client, "rate", rate_code, futures["trade_date"].max(), source="sina")
            results.append({"entity": "rate", "rate_code": rate_code, "rows": n})
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "rate", "rate_code": rate_code, "error": str(exc)})
    return results


def _refresh_indexes(client, registry, progress: SyncProgress | None = None) -> list[dict]:
    """指数层-行情：价格/全收益日行情 + 水位（000300S 用 H00300 拉取）。

    增量：从「该指数水位 - OVERLAP_DAYS」起拉，空则全量。
    """
    from src.fetchers.index_valuation_fetcher import fetch_index_daily_history
    from src.storage.supabase_store import upsert_index_daily_history

    symbol_map = {"000300S": "H00300"}
    results: list[dict] = []
    wm = _watermark_map(client)
    for index_code in sorted(registry.keys()):
        if progress:
            progress.step(f"指数 {index_code}：日行情")
        try:
            since = _since_date(wm, ("index", index_code))
            df = fetch_index_daily_history(symbol_map.get(index_code, index_code), start_date=since or "20000101")
            if not df.empty:
                df["index_code"] = index_code
                n = upsert_index_daily_history(client, df)
                if "trade_date" in df.columns:
                    upsert_watermark(client, "index", index_code, df["trade_date"].max(), source="stock_zh_index_value_csindex")
                results.append({"entity": "index", "index_code": index_code, "daily_rows": n})
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "index", "index_code": index_code, "error": str(exc)})
    return results


def _refresh_valuation(
    client, progress: SyncProgress | None = None, index_codes: tuple[str, ...] = ("H30269", "000300")
) -> list[dict]:
    """指数层-估值：官方近20日累积 + 推导历史前缀（source 标注）。

    增量：官方近 20 日每次都拉（数据量小）；推导前缀（H30269 需全量重拉 H30269/H20269，较重）
    只在「无推导数据 或 已覆盖落后官方起点 30 天以上」时重算，已覆盖则跳过。
    """
    import akshare as _ak

    from src.fetchers.index_valuation_fetcher import derive_index_dividend_yield
    from src.storage.supabase_store import _fetch_all_rows

    results: list[dict] = []
    for index_code in index_codes:
        if progress:
            progress.step(f"指数 {index_code}：估值")
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

                # 推导前缀（只补官方之前，source='derived'）——增量已覆盖则跳过
                if index_code == "H30269":
                    official_min = official["trade_date"].min()
                    derived_max = None
                    rows = (
                        client.table("index_valuation_history")
                        .select("trade_date")
                        .eq("index_code", "H30269")
                        .eq("source", "derived")
                        .order("trade_date", desc=True)
                        .limit(1)
                        .execute()
                    ).data or []
                    if rows:
                        derived_max = pd.Timestamp(rows[0]["trade_date"])
                    need_derived = derived_max is None or derived_max < official_min - pd.Timedelta(days=30)
                    if need_derived:
                        derived = derive_index_dividend_yield(index_code)
                        if not derived.empty:
                            prefix = derived[derived["trade_date"] < official_min].copy()
                            prefix = prefix.rename(columns={"dividend_yield1": "dividend_yield"})
                            prefix["index_code"] = index_code
                            n_derived = upsert_index_valuations(client, prefix, source="derived")
                            results.append({"entity": "valuation", "index_code": index_code, "derived_rows": n_derived})
        except Exception as exc:  # noqa: BLE001
            results.append({"entity": "valuation", "index_code": index_code, "error": str(exc)})
    return results


def _refresh_factors(client, progress: SyncProgress | None = None) -> list[dict]:
    """策略层：指数日频因子重算（波动/回撤取全收益 H20269）。"""
    from src.indicators.strategy_factors import compute_index_factors
    from src.storage.supabase_store import (
        _fetch_all_rows,
        delete_index_daily_factors,
        upsert_index_daily_factors,
    )

    if progress:
        progress.step("策略层：指数日频因子重算")
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


def refresh_all(client, progress: SyncProgress | None = None) -> list[dict]:
    """新 ER 结构完整刷新（UI「刷新数据」按钮用）。

    顺序：配置 → 基金(档案/净值/分红) → cn_10y → 指数行情(价格+全收益) → 指数估值(官方+推导) → 策略因子。

    :param progress: 可选进度对象（SyncProgress 或 None）；CLI/无 UI 调用时传 None 即静默。
    """
    results: list[dict] = []
    fund_codes = [normalize_fund_code(code) for code in load_fund_codes()]
    registry = load_index_registry()
    valuation_codes = ("H30269", "000300")
    # 基金层步数 = 每只基金净值 + 分红合并 1 步 + 档案 1 步
    total = 1 + (len(fund_codes) + 2) + 1 + len(registry) + len(valuation_codes) + 1
    tracker = progress if isinstance(progress, SyncProgress) else SyncProgress(total, progress)
    tracker.report(0, "准备同步…")

    tracker.report(0, "① 同步配置（指数注册表 / 基金→指数映射）")
    try:
        results.append(sync_config(client))
    except Exception as exc:  # noqa: BLE001
        results.append({"entity": "config", "error": str(exc)})

    tracker.report(1, "② 基金层：净值 / 分红 / 档案")
    results += _refresh_funds(client, fund_codes, tracker)

    tracker.report(1 + len(fund_codes) + 2, "③ 宏观层：cn_10y 利率")
    results += _refresh_rate(client, tracker)

    tracker.report(2 + len(fund_codes) + 2, "④ 指数层：价格 / 全收益行情")
    results += _refresh_indexes(client, registry, tracker)

    tracker.report(2 + len(fund_codes) + 2 + len(registry), "⑤ 指数层：估值（官方 + 推导）")
    results += _refresh_valuation(client, tracker, index_codes=valuation_codes)

    tracker.report(total - 1, "⑥ 策略层：指数日频因子")
    results += _refresh_factors(client, tracker)

    tracker.report(total, "同步完成")

    insert_sync_log(
        client,
        job_name="refresh_all",
        status="success" if not any("error" in r for r in results) else "partial",
        message=f"Refreshed {len(fund_codes)} funds + index/rate/valuation/factors",
        row_count=0,
    )
    return results


LAYER_KEYS = ("fund", "index", "rate", "factors")


def refresh_layer(client, layer_key: str, progress: SyncProgress | None = None) -> tuple[list[dict], str | None]:
    """按层刷新（UI「按层刷新」按钮用），返回 (结果列表, 错误信息)。

    - fund → 基金净值/分红/档案（同步水位补齐）
    - rate → cn_10y 利率
    - index → 指数行情 + 估值（官方+推导）
    - factors → 策略因子重算

    :param progress: 可选进度对象（SyncProgress 或 None）；CLI/无 UI 调用时传 None 即静默。
    """
    layer_key = (layer_key or "").lower()
    if layer_key == "fund":
        fund_codes = [normalize_fund_code(code) for code in load_fund_codes()]
        tracker = SyncProgress(len(fund_codes) + 2, progress)
        tracker.report(0, f"基金层：共 {len(fund_codes)} 只基金")
        results = _refresh_funds(client, fund_codes, tracker)
    elif layer_key == "rate":
        tracker = SyncProgress(3, progress)
        tracker.report(0, "宏观层：cn_10y 利率 + 国债期货 TF/T")
        results = _refresh_rate(client, tracker)
    elif layer_key == "index":
        registry = load_index_registry()
        valuation_codes = ("H30269", "000300")
        tracker = SyncProgress(len(registry) + len(valuation_codes), progress)
        tracker.report(0, "指数层：日行情")
        results = _refresh_indexes(client, registry, tracker)
        tracker.report(len(registry), "指数层：估值（官方 + 推导）")
        results += _refresh_valuation(client, tracker, index_codes=valuation_codes)
    elif layer_key == "factors":
        tracker = SyncProgress(1, progress)
        tracker.report(0, "策略层：指数日频因子重算")
        results = _refresh_factors(client, tracker)
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
