"""RSI 相对强弱指标（红利低波量化看板）核心计算。

口径（对齐用户方案）：
- 日 RSI：以基金复权净值（adjusted_nav）为价格，Wilder 平滑法，周期 6 / 12。
- 周 RSI：复权净值按周（W-FRI）重采样取周收盘，Wilder 平滑法，周期 12 / 24。
- 250 MA：复权净值 250 交易日滚动均线；偏离度 = (净值 - 250MA) / 250MA。
- 股息率 Spread：红利低波指数股息率 - cn_10y（直接复用 index_daily_factors.spread）。
- 信号规则（模式 A/B/C，详见模块内函数注释）：
    A 周/日 RSI 共振低吸：周RSI<35 且 日RSI 上穿 30，净值在 250MA 附近或下方 → 强烈买入
    B 底背离反弹：净值创近 6 月新低，但周RSI 低点比上一次更高 → 分批定投
    C 钝化陷阱：周RSI>65 且净值沿 250MA 上方稳步上涨且 Spread 高于历史均值 → 勿止盈
- 前瞻统计：信号后 T+20/60/120 交易日累计收益 + 未来 60 日最大回撤（验证胜率）。

全部为派生计算，不落库（符合项目「派生不落库、实时算」纪律）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# RSI 阈值（用户方案）
RSI_LOW = 35.0    # 周 RSI 低吸阈值
RSI_HIGH = 65.0   # 周 RSI 风险阈值
DAILY_CROSS = 30.0  # 日 RSI 上穿阈值（跌破后向上弯头）
MA_WINDOW = 250       # 250 日移动平均线
SIX_MONTH_WEEKS = 26  # 近 6 个月（周）新低窗口
SIGNAL_SPACING = 21   # 同类型信号最小间隔（交易日），避免图表标注过密
FORWARD_OFFSETS = {"fwd_20": 20, "fwd_60": 60, "fwd_120": 120}  # T+20/60/120

# 信号类型 → 中文标签
SIGNAL_LABELS = {
    "A": "A 周/日共振低吸（强烈买入）",
    "B": "B 底背离反弹（分批定投）",
    "C": "C 钝化陷阱（勿止盈）",
}


def wilders_rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder 平滑 RSI（0~100）。

    :param close: 价格序列（按时间升序，NaN 已剔除）。
    :param period: 周期（6/12/24 等）。
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    valid = avg_gain.notna() & avg_loss.notna()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi = 100.0 - 100.0 / (1.0 + rs)
    # 只有上涨（avg_loss=0）→ RSI=100；只有下跌（avg_gain=0）→ RSI=0；数据不足 → NaN
    rsi = rsi.where(valid, np.nan)
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


def _clean_price(nav_df: pd.DataFrame, col: str = "adjusted_nav") -> pd.DataFrame:
    """净值 → 干净的价格日序列（nav_date 升序、去重、剔除缺失）。"""
    if nav_df is None or nav_df.empty or col not in nav_df.columns:
        return pd.DataFrame(columns=["nav_date", col])
    d = nav_df[["nav_date", col]].copy()
    d["nav_date"] = pd.to_datetime(d["nav_date"], errors="coerce")
    d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["nav_date", col]).drop_duplicates("nav_date").sort_values("nav_date").reset_index(drop=True)
    return d


def _build_daily(nav_df: pd.DataFrame) -> pd.DataFrame:
    """日度指标：复权净值 + 250MA + 偏离度 + 日 RSI(6/12)。"""
    d = _clean_price(nav_df)
    if d.empty:
        return d
    close = d["adjusted_nav"]
    d["ma250"] = close.rolling(MA_WINDOW).mean()
    d["deviation"] = (close / d["ma250"] - 1.0) * 100.0
    d["rsi6"] = wilders_rsi(close, 6)
    d["rsi12"] = wilders_rsi(close, 12)
    return d


def _build_weekly(nav_df: pd.DataFrame) -> pd.DataFrame:
    """周度指标：周收盘（W-FRI 重采样）+ 周 RSI(12/24)。"""
    d = _clean_price(nav_df)
    if d.empty:
        return pd.DataFrame(columns=["nav_date", "close", "rsi12", "rsi24"])
    w = d.set_index("nav_date")["adjusted_nav"].resample("W-FRI").last().dropna()
    weekly = pd.DataFrame(
        {
            "close": w,
            "rsi12": wilders_rsi(w, 12),
            "rsi24": wilders_rsi(w, 24),
        }
    )
    weekly.index.name = "nav_date"
    return weekly.reset_index()


def _map_weekly_to_daily(daily: pd.DataFrame, weekly: pd.DataFrame) -> pd.DataFrame:
    """周 RSI 前向填充映射到日线（共享 x 轴绘图用）。"""
    daily = daily.copy()
    if weekly.empty:
        daily["w_rsi12"] = np.nan
        daily["w_rsi24"] = np.nan
        return daily
    w = weekly.set_index("nav_date")
    idx = daily["nav_date"]
    daily["w_rsi12"] = w["rsi12"].reindex(idx, method="ffill").to_numpy()
    daily["w_rsi24"] = w["rsi24"].reindex(idx, method="ffill").to_numpy()
    return daily


def _build_spread_daily(daily: pd.DataFrame, factors_df: pd.DataFrame) -> tuple[pd.Series | None, pd.Series | None]:
    """股息率利差（spread）从指数层因子前向填充到日线；无数据返回 (None, None)。"""
    if factors_df is None or factors_df.empty or "spread" not in factors_df.columns:
        return None, None
    sp = factors_df[["trade_date", "spread"]].copy()
    sp["trade_date"] = pd.to_datetime(sp["trade_date"], errors="coerce")
    sp["spread"] = pd.to_numeric(sp["spread"], errors="coerce")
    sp = sp.dropna(subset=["trade_date", "spread"]).drop_duplicates("trade_date").sort_values("trade_date")
    if sp.empty:
        return None, None
    sp = sp.set_index("trade_date")["spread"]
    spread_daily = sp.reindex(daily["nav_date"], method="ffill")
    spread_mean = spread_daily.expanding(min_periods=60).mean()
    return spread_daily, spread_mean


def _detect_signals(
    daily: pd.DataFrame,
    spread_daily: pd.Series | None,
    spread_mean: pd.Series | None,
) -> list[dict]:
    """模式 A / C 信号检测（逐日），返回信号字典列表。

    - A 周/日 RSI 共振低吸：w_rsi12 < 35 且 (日 RSI6 或 RSI12 上穿 30) 且 净值 <= 250MA×1.03
    - C 钝化陷阱：w_rsi12 > 65 且 净值 > 250MA 且 60 日稳步上涨 且 (有 spread 时) spread >= 历史均值
    """
    d = daily.copy()
    signals: list[dict] = []

    # ---- 模式 A ----
    rsi6, rsi12 = d["rsi6"], d["rsi12"]
    cross_up = ((rsi6.shift(1) <= DAILY_CROSS) & (rsi6 > DAILY_CROSS)) | (
        (rsi12.shift(1) <= DAILY_CROSS) & (rsi12 > DAILY_CROSS)
    )
    near_or_below_ma = d["adjusted_nav"] <= d["ma250"] * 1.03
    cond_a = (d["w_rsi12"] < RSI_LOW) & cross_up & near_or_below_ma & d["ma250"].notna()
    for idx in d.index[cond_a.fillna(False)]:
        signals.append(
            {
                "nav_date": d.at[idx, "nav_date"],
                "kind": "A",
                "weekly_rsi": float(d.at[idx, "w_rsi12"]),
                "daily_rsi": float(d.at[idx, "rsi6"]) if pd.notna(d.at[idx, "rsi6"]) else float(d.at[idx, "rsi12"]),
                "nav": float(d.at[idx, "adjusted_nav"]),
                "ma250": float(d.at[idx, "ma250"]),
            }
        )

    # ---- 模式 C ----
    rising = d["adjusted_nav"] >= d["adjusted_nav"].shift(60)
    cond_c = (d["w_rsi12"] > RSI_HIGH) & (d["adjusted_nav"] > d["ma250"]) & rising & d["ma250"].notna()
    if spread_daily is not None and spread_mean is not None:
        cond_c &= (spread_daily >= spread_mean)
    for idx in d.index[cond_c.fillna(False)]:
        signals.append(
            {
                "nav_date": d.at[idx, "nav_date"],
                "kind": "C",
                "weekly_rsi": float(d.at[idx, "w_rsi12"]),
                "daily_rsi": float(d.at[idx, "rsi6"]) if pd.notna(d.at[idx, "rsi6"]) else float(d.at[idx, "rsi12"]),
                "nav": float(d.at[idx, "adjusted_nav"]),
                "ma250": float(d.at[idx, "ma250"]),
            }
        )

    # 同类型信号最小间隔去重（按时间升序）
    signals.sort(key=lambda s: s["nav_date"])
    deduped: list[dict] = []
    for sig in signals:
        prev = next((s for s in reversed(deduped) if s["kind"] == sig["kind"]), None)
        if prev is not None and (sig["nav_date"] - prev["nav_date"]).days < SIGNAL_SPACING:
            continue
        deduped.append(sig)
    return deduped


def _detect_divergences(weekly: pd.DataFrame) -> list[dict]:
    """模式 B：底背离（净值创近 6 月新低，但周 RSI 低点比上一次更高）。

    返回背离点列表（含连接两低点的起止坐标，供主图/动能图绘制背离线）。
    """
    if weekly.empty or len(weekly) < 8 or "rsi12" not in weekly.columns:
        return []
    w = weekly.copy()
    w["nav_date"] = pd.to_datetime(w["nav_date"], errors="coerce")
    w = w.dropna(subset=["nav_date"]).set_index("nav_date").sort_index()
    r = w["rsi12"]
    valid = r.notna()
    is_min = valid & (r < r.shift(1)) & (r <= r.shift(-1))
    troughs = w[is_min].reset_index()
    divergences: list[dict] = []
    for i in range(1, len(troughs)):
        prev = troughs.iloc[i - 1]
        curr = troughs.iloc[i]
        if curr["close"] >= prev["close"] or curr["rsi12"] <= prev["rsi12"]:
            continue
        # 净值须创近 6 个月（26 周）新低
        history = w.loc[: curr["nav_date"], "close"]
        six_mo_low = history.tail(SIX_MONTH_WEEKS).min() if len(history) >= SIX_MONTH_WEEKS else history.min()
        if curr["close"] > six_mo_low:
            continue
        divergences.append(
            {
                "nav_date": curr["nav_date"],
                "price_x_prev": prev["nav_date"],
                "price_y_prev": float(prev["close"]),
                "price_x_curr": curr["nav_date"],
                "price_y_curr": float(curr["close"]),
                "rsi_x_prev": prev["nav_date"],
                "rsi_y_prev": float(prev["rsi12"]),
                "rsi_x_curr": curr["nav_date"],
                "rsi_y_curr": float(curr["rsi12"]),
                "weekly_rsi": float(curr["rsi12"]),
                "nav": float(curr["close"]),
            }
        )
    return divergences


def _add_forward_stats(daily: pd.DataFrame, signals: list[dict]) -> list[dict]:
    """为每个信号补充前瞻收益（T+20/60/120）与未来 60 日最大回撤。"""
    if not signals:
        return signals
    close = daily["adjusted_nav"].to_numpy(dtype=float)
    idx_by_date = {ts: i for i, ts in enumerate(daily["nav_date"])}
    result: list[dict] = []
    for sig in signals:
        i = idx_by_date.get(sig["nav_date"])
        if i is None:
            continue
        out = dict(sig)
        base = close[i]
        for key, offset in FORWARD_OFFSETS.items():
            j = i + offset
            out[key] = (close[j] / base - 1.0) * 100.0 if j < len(close) else None
        # 未来 60 交易日内最大回撤（接飞刀风险）
        window = close[i + 1 : min(i + 1 + 60, len(close))]
        if len(window):
            running_max = np.maximum.accumulate(window)
            out["mdd60"] = float((window / running_max - 1.0).min() * 100.0)
        else:
            out["mdd60"] = None
        result.append(out)
    return result


def _signal_label(kind: str) -> str:
    return SIGNAL_LABELS.get(kind, kind)


def _latest_signal_text(daily: pd.DataFrame, spread_daily: pd.Series | None, spread_mean: pd.Series | None) -> str | None:
    """基于最新一日的规则做「当前组合判断」文案。"""
    if daily.empty:
        return None
    row = daily.iloc[-1]
    w_rsi = row.get("w_rsi12")
    if pd.isna(w_rsi):
        return None
    nav, ma250 = row.get("adjusted_nav"), row.get("ma250")
    rsi6, rsi12 = row.get("rsi6"), row.get("rsi12")

    if w_rsi < RSI_LOW:
        zone = "超卖区"
        guide = "可分批低吸，等日 RSI 上穿 30 确认后加仓"
    elif w_rsi > RSI_HIGH:
        zone = "超买区"
        if pd.notna(ma250) and nav > ma250 and pd.notna(rsi6) and rsi6 > rsi12:
            if spread_daily is not None and spread_mean is not None and pd.notna(spread_daily.iloc[-1]):
                guide = "高位钝化（利差仍高于均值）——勿止盈，仅暂停加仓" if spread_daily.iloc[-1] >= spread_mean.iloc[-1] else "注意回调风险，可考虑分批止盈"
            else:
                guide = "高位钝化风险，勿追高"
        else:
            guide = "注意回调风险"
    else:
        zone = "中性区"
        guide = "动能平稳，等待周 RSI 进入 <35 或 >65 再行动"
    return f"周RSI {w_rsi:.1f}（{zone}）：{guide}"


def build_rsi_dashboard(nav_df: pd.DataFrame, factors_df: pd.DataFrame | None = None) -> dict:
    """构建 RSI 看板所需全部数据（派生计算，不落库）。

    :param nav_df: 基金净值（含 nav_date / adjusted_nav，升序），取全历史或已按范围裁剪。
    :param factors_df: 该基金策略底层指数的 index_daily_factors（含 trade_date/spread），可为空。
    :return: dict(nav, daily, weekly, spread, signals, divergences, stats, latest)
    """
    daily = _build_daily(nav_df)
    if daily.empty:
        return {
            "nav": pd.DataFrame(),
            "daily": pd.DataFrame(),
            "weekly": pd.DataFrame(),
            "spread": pd.DataFrame(),
            "signals": pd.DataFrame(),
            "divergences": [],
            "stats": {},
            "latest": {},
        }
    weekly = _build_weekly(nav_df)
    daily = _map_weekly_to_daily(daily, weekly)

    spread_daily, spread_mean = _build_spread_daily(daily, factors_df)
    signals = _detect_signals(daily, spread_daily, spread_mean)
    divergences = _detect_divergences(weekly)
    # 背离点并入信号（kind=B，同样参与前瞻收益 / 回撤统计）
    signals = signals + [
        {
            "nav_date": dv["nav_date"],
            "kind": "B",
            "weekly_rsi": dv["weekly_rsi"],
            "daily_rsi": np.nan,
            "nav": dv["nav"],
            "ma250": np.nan,
        }
        for dv in divergences
    ]
    signals = _add_forward_stats(daily, signals)

    # ---- 输出 DataFrame ----
    sig_df = pd.DataFrame(signals)
    if not sig_df.empty:
        sig_df = sig_df.sort_values("nav_date").reset_index(drop=True)
        sig_df["label"] = sig_df["kind"].map(_signal_label)

    spread_df = pd.DataFrame()
    if spread_daily is not None:
        spread_df = pd.DataFrame({"nav_date": daily["nav_date"], "spread": spread_daily.to_numpy()}).dropna(subset=["spread"])

    # ---- 统计（买入信号 A/B 的前瞻胜率） ----
    stats = _compute_stats(sig_df)

    # ---- 最新状态 ----
    last = daily.iloc[-1]
    latest = {
        "nav_date": last["nav_date"],
        "nav": float(last["adjusted_nav"]),
        "ma250": float(last["ma250"]) if pd.notna(last["ma250"]) else None,
        "deviation": float(last["deviation"]) if pd.notna(last["deviation"]) else None,
        "rsi6": float(last["rsi6"]) if pd.notna(last["rsi6"]) else None,
        "rsi12": float(last["rsi12"]) if pd.notna(last["rsi12"]) else None,
        "w_rsi12": float(last["w_rsi12"]) if pd.notna(last["w_rsi12"]) else None,
        "w_rsi24": float(last["w_rsi24"]) if pd.notna(last["w_rsi24"]) else None,
        "spread": float(spread_daily.iloc[-1]) if spread_daily is not None and pd.notna(spread_daily.iloc[-1]) else None,
        "signal_text": _latest_signal_text(daily, spread_daily, spread_mean),
    }

    return {
        "nav": daily[["nav_date", "adjusted_nav", "ma250", "deviation"]],
        "daily": daily[["nav_date", "rsi6", "rsi12"]],
        "weekly": daily[["nav_date", "w_rsi12", "w_rsi24"]],
        "spread": spread_df,
        "signals": sig_df,
        "divergences": divergences,
        "stats": stats,
        "latest": latest,
    }


def _compute_stats(sig_df: pd.DataFrame) -> dict:
    """信号统计：买入信号(A/B) 数量 + 前瞻收益/回撤/胜率。"""
    stats: dict = {}
    if sig_df is None or sig_df.empty:
        return stats
    buy = sig_df[sig_df["kind"].isin(["A", "B"])]
    if not buy.empty:
        for key in FORWARD_OFFSETS:
            vals = buy[key].dropna()
            if not vals.empty:
                stats[f"avg_{key}"] = float(vals.mean())
        win = buy["fwd_60"].dropna()
        if not win.empty:
            stats["win_rate_60"] = float((win > 0).mean() * 100.0)
        mdd = buy["mdd60"].dropna()
        if not mdd.empty:
            stats["avg_mdd60"] = float(mdd.mean())
        stats["buy_count"] = int(len(buy))
    stats["signal_count"] = int(len(sig_df))
    return stats


def slice_rsi_dashboard(data: dict, start_ts: pd.Timestamp | None = None) -> dict:
    """把全历史计算的看板数据按显示起点裁剪（图表/信号表只展示窗口内数据）。

    **关键设计**：所有指标（250MA/RSI/信号）都基于全历史计算后才裁剪，
    因此显示窗口起始处即有正确取值，不会出现「前一年无 250 均线」或
    「窗口截断导致均线被高起点拉偏成拟合线」的伪影。

    :param data: build_rsi_dashboard 的返回 dict（全历史口径）。
    :param start_ts: 显示起始日（None 表示不裁剪）。
    """
    if start_ts is None:
        return data
    out: dict = {}
    for key in ("nav", "daily", "weekly", "spread"):
        df = data.get(key)
        if df is not None and not df.empty:
            out[key] = df[df["nav_date"] >= start_ts].reset_index(drop=True)
        else:
            out[key] = df
    sig = data.get("signals")
    if sig is not None and not sig.empty:
        out["signals"] = sig[sig["nav_date"] >= start_ts].reset_index(drop=True)
    else:
        out["signals"] = sig
    out["divergences"] = [dv for dv in data.get("divergences", []) if dv["nav_date"] >= start_ts]
    # 统计按窗口内信号重算（与显示范围口径一致）；latest 取最新一日，与范围无关
    out["stats"] = _compute_stats(out["signals"])
    out["latest"] = data.get("latest")
    return out
