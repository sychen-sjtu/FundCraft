"""临时测试：国债期货加仓信号 vs 直接定投（多阈值敏感性对比）。

标的：007171（易方达中债3-5年国开行债A）
比较口径（保证公平）：
- 信号策略：历史买入点位（mark_buy_points，当日收盘口径）每个点位买 AMOUNT_PER_SIGNAL 元
- 直接定投：总投入 = 信号策略总投入（同投入才能直接比市值），按每月固定日（DCA_DAY）均摊
- 份额统一用复权净值（adjusted_nav，含分红再投资口径）
- 期末市值 = 累计份额 × 期末复权净值；年化用 XIRR

用法：python scripts/compare_signal_vs_dca.py [--tf 0.10 0.15] [--show-detail 0.15]
默认对比 TF 优选阈值 0.10 与 0.15（强化带按比例平移、T 阈值等比放大）。
"""

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_supabase_settings, supabase_settings_ready
from src.indicators.bond_signal import LEVEL_LABELS, mark_buy_points
from src.storage.supabase_store import create_supabase_client, fetch_macro_rates, fetch_nav_history

FUND_CODE = "007171"
AMOUNT_PER_SIGNAL = 200.0  # 每次信号买入金额
DCA_DAY = 15  # 定投每月固定日

# 默认对比场景：(标签, tf_preferred, tf_strengthen_low, tf_strengthen_high, t_strengthen)
SCENARIOS = [
    ("TF阈值 0.10%（当前）", -0.10, -0.08, -0.10, -0.15),
    ("TF阈值 0.15%（增强）", -0.15, -0.12, -0.15, -0.20),
]


def _xirr(cashflows: list[tuple[object, float]], guess: float = 0.03) -> float | None:
    """XIRR：现金流年化内部收益率（流入为正、流出为负），二分求根。"""
    dates = pd.to_datetime([c[0] for c in cashflows])
    amounts = np.array([c[1] for c in cashflows], dtype=float)
    if len(dates) < 2 or (amounts >= 0).all() or (amounts <= 0).all():
        return None
    days = (dates - dates[0]).days.to_numpy(dtype=float) / 365.25

    def npv(rate):
        return float(np.sum(amounts / (1.0 + rate) ** days))

    low, high = -0.9999, max(guess, 0.0001)
    if npv(low) * npv(high) > 0:
        for _ in range(80):
            high *= 2.0
            if npv(low) * npv(high) <= 0:
                break
        if npv(low) * npv(high) > 0:
            return None
    for _ in range(300):
        mid = (low + high) / 2.0
        if npv(mid) == 0.0:
            return mid
        if npv(low) * npv(mid) < 0:
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def _nav_on_or_after(dates: np.ndarray, nav_df: pd.DataFrame) -> np.ndarray:
    nav_dates = nav_df["nav_date"].values.astype("datetime64[D]")
    nav_vals = nav_df["adjusted_nav"].values.astype(float)
    idx = np.searchsorted(nav_dates, dates.astype("datetime64[D]"), side="left")
    idx = np.clip(idx, 0, len(nav_vals) - 1)
    return nav_vals[idx]


def run_signal(points, nav, start, end, amount):
    """信号策略：返回 (买点数, 总投入, 期末市值, XIRR, 买点df)。"""
    buys = points[
        (points["trigger"])
        & (points["trade_date"].dt.date >= start)
        & (points["trade_date"].dt.date <= end)
    ].sort_values("trade_date")
    n = len(buys)
    total = amount * n
    if n == 0:
        return 0, 0.0, 0.0, None, buys
    navs = _nav_on_or_after(buys["trade_date"].values, nav)
    shares = float(np.sum(amount / navs))
    window = nav[(nav["nav_date"].dt.date >= start) & (nav["nav_date"].dt.date <= end)]
    end_nav = float(window["adjusted_nav"].iloc[-1])
    end_date = window["nav_date"].iloc[-1].date()
    final = shares * end_nav
    cf = [(d.date(), -amount) for d in buys["trade_date"]] + [(end_date, final)]
    return n, total, final, _xirr(cf), buys


def run_dca(nav, start, end, total_signal):
    """直接定投：总投入=total_signal，每月固定日均摊。返回 (期数, 单期, 期末市值, XIRR)。"""
    window = nav[(nav["nav_date"].dt.date >= start) & (nav["nav_date"].dt.date <= end)]
    end_date = window["nav_date"].iloc[-1].date()
    end_nav = float(window["adjusted_nav"].iloc[-1])
    month_starts = pd.date_range(start, end_date, freq="MS")
    dca_dates = [ms.replace(day=DCA_DAY) for ms in month_starts]
    dca_dates = [d for d in dca_dates if start <= d.date() <= end_date]
    n = len(dca_dates)
    if n == 0 or total_signal <= 0:
        return 0, 0.0, 0.0, None
    per = total_signal / n
    navs = _nav_on_or_after(np.array([np.datetime64(d) for d in dca_dates]), nav)
    shares = float(np.sum(per / navs))
    final = shares * end_nav
    cf = [(d.date(), -per) for d in dca_dates] + [(end_date, final)]
    return n, per, final, _xirr(cf)


def run_lump_sum(nav, start, end, total):
    """一次性买入持有：窗口起点满仓 total 元持有到期末。返回 (买入日, 期末市值, XIRR)。"""
    window = nav[(nav["nav_date"].dt.date >= start) & (nav["nav_date"].dt.date <= end)]
    if window.empty or total <= 0:
        return None, 0.0, None
    first = window.iloc[0]
    end_row = window.iloc[-1]
    buy_date = first["nav_date"].date()
    end_date = end_row["nav_date"].date()
    buy_nav = float(first["adjusted_nav"])
    end_nav = float(end_row["adjusted_nav"])
    shares = total / buy_nav
    final = shares * end_nav
    cf = [(buy_date, -total), (end_date, final)]
    return buy_date, final, _xirr(cf)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf", nargs="+", type=float, default=[0.10, 0.15], help="要对比的 TF 优选阈值(%)")
    parser.add_argument("--show-detail", type=float, default=None, help="打印该阈值下的买点明细")
    parser.add_argument("--years", type=float, default=1.0, help="回测窗口年数")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    password = os.environ.get("FUNDCRAFT_SECRET_PASSPHRASE", "").strip() or "sychen"
    settings = load_supabase_settings(root, secret_password=password)
    if not supabase_settings_ready(settings):
        raise SystemExit("Supabase 配置缺失")
    client = create_supabase_client(settings)

    tf_daily = fetch_macro_rates(client, "bond_futures_tf").rename(columns={"rate_date": "trade_date"})
    t_daily = fetch_macro_rates(client, "bond_futures_t").rename(columns={"rate_date": "trade_date"})
    nav = fetch_nav_history(client, FUND_CODE)
    nav["nav_date"] = pd.to_datetime(nav["nav_date"], errors="coerce")
    nav = nav.dropna(subset=["nav_date", "adjusted_nav"]).sort_values("nav_date").reset_index(drop=True)

    end = date.today()
    start = end - timedelta(days=int(args.years * 365))
    window = nav[(nav["nav_date"].dt.date >= start) & (nav["nav_date"].dt.date <= end)]
    if window.empty:
        raise SystemExit("窗口内无净值数据")
    end_date = window["nav_date"].iloc[-1].date()
    start_nav = float(window["adjusted_nav"].iloc[0])
    end_nav = float(window["adjusted_nav"].iloc[-1])
    print(f"标的：{FUND_CODE}　窗口：{start} ~ {end_date}（近 {args.years:.0f} 年）")
    print(f"复权净值：{start_nav:.4f} → {end_nav:.4f}（期间 {((end_nav / start_nav - 1) * 100):+.2f}%，买入持有参考）\n")

    # 各场景信号买点（全历史算，切窗口）
    scenario_buys = {}
    for label, pref, slow, shigh, tst in SCENARIOS:
        if abs(pref) not in [abs(t) for t in args.tf]:
            continue
        points = mark_buy_points(
            tf_daily, t_daily, nav,
            tf_preferred=pref, tf_strengthen_low=slow, tf_strengthen_high=shigh, t_strengthen=tst,
        )
        scenario_buys[label] = (points, pref, slow, shigh, tst)

    rows = []
    for label, (points, pref, slow, shigh, tst) in scenario_buys.items():
        n_sig, total_sig, final_sig, xirr_sig, buys = run_signal(points, nav, start, end_date, AMOUNT_PER_SIGNAL)
        n_dca, per_dca, final_dca, xirr_dca = run_dca(nav, start, end_date, total_sig)
        lump_date, final_lump, xirr_lump = run_lump_sum(nav, start, end_date, total_sig)
        rows.append((label, n_sig, total_sig, final_sig, xirr_sig, n_dca, per_dca, final_dca, xirr_dca, final_lump, xirr_lump, lump_date, buys))

    def _pct(x):
        return "—" if x is None else f"{x * 100:.2f}%"

    header = f"{'项目':<22}"
    for r in rows:
        header += f"{r[0]:>22}"
    print(header)
    print("-" * (22 + 22 * len(rows)))
    print(f"{'买入次数':<22}" + "".join(f"{r[1]:>22}" for r in rows))
    print(f"{'单次金额(元)':<22}" + "".join(f"{AMOUNT_PER_SIGNAL:>22.0f}" for r in rows))
    print(f"{'总投入(元)':<22}" + "".join(f"{r[2]:>22.0f}" for r in rows))
    print(f"{'定投期数':<22}" + "".join(f"{r[5]:>22}" for r in rows))
    print(f"{'定投市值(元)':<22}" + "".join(f"{r[7]:>22.2f}" for r in rows))
    print(f"{'信号市值(元)':<22}" + "".join(f"{r[3]:>22.2f}" for r in rows))
    print(f"{'一次性市值(元)':<22}" + "".join(f"{r[9]:>22.2f}" for r in rows))
    print(f"{'信号总收益率':<22}" + "".join(f"{_pct(r[3] / r[2] - 1):>22}" for r in rows))
    print(f"{'一次性总收益率':<22}" + "".join(f"{_pct(r[9] / r[2] - 1):>22}" for r in rows))
    print(f"{'信号XIRR':<22}" + "".join(f"{_pct(r[4]):>22}" for r in rows))
    print(f"{'定投XIRR':<22}" + "".join(f"{_pct(r[8]):>22}" for r in rows))
    print(f"{'一次性XIRR':<22}" + "".join(f"{_pct(r[10]):>22}" for r in rows))
    print("-" * (22 + 22 * len(rows)))
    for r in rows:
        print(f"{r[0]}: 买入日 {r[11]} 起 {r[2]:,.0f} 元 → 一次性 {r[9]:,.2f} / 信号 {r[3]:,.2f} / 定投 {r[7]:,.2f}（信号-定投 {r[3]-r[7]:+,.2f} 元 / {_pct((r[3]-r[7]) / r[2])}）")

    # 买点明细（指定阈值）
    show_pref = args.show_detail
    for label, (points, pref, slow, shigh, tst) in scenario_buys.items():
        if show_pref is not None and abs(pref) != abs(show_pref):
            continue
        buys = points[
            (points["trigger"]) & (points["trade_date"].dt.date >= start) & (points["trade_date"].dt.date <= end_date)
        ].sort_values("trade_date")
        print(f"\n[{label} 买点明细]（日期 / 触发 / 买入复权净值）共 {len(buys)} 次")
        for row in buys.itertuples():
            nv = _nav_on_or_after(np.array([np.datetime64(row.trade_date)]), nav)[0]
            print(f"  {pd.Timestamp(row.trade_date).date()}　{LEVEL_LABELS.get(row.level, row.level):<6}　{nv:.4f}")


if __name__ == "__main__":
    main()
