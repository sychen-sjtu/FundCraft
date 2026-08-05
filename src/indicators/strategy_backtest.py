"""策略回测：基于 008163 日频因子信号，模拟"得分≥阈值买入 6000 元"并计算绩效。

回测模型（对应 docs 设计稿 §五 + gemini-code 文档）：
1. 每日收盘后按因子得分判定买入信号（score >= threshold，默认 80）。
2. 触发时投入 buy_amount（默认 6000 元）买入基金份额（当日净值口径，场外简化）。
3. 闲置现金按 cn_10y 日利率计息（rate/100 / 365，逐日复利）。
4. 绩效：XIRR（不规则现金流内部年化收益率）+ 组合净值最大回撤。

说明：
- 默认模型把每次触发买入的 6000 元视为"外部投入"（负现金流），期末组合市值
  视为正现金流，以此计算 XIRR，与常见择时买入口径一致。
- 若传入 start_cash > 0，则期初现金作为一笔负现金流参与 XIRR，且闲置现金
  会真实计息。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_BUY_AMOUNT = 6000.0
DEFAULT_THRESHOLD = 80.0


def _xirr(cash_flows: list[tuple[pd.Timestamp, float]], *, lo: float = -0.9999, hi: float = 20.0) -> float:
    """用二分法求解不规则现金流的内部年化收益率（小数，如 0.09 = 9%）。"""
    if len(cash_flows) < 2:
        return 0.0

    base_date = cash_flows[0][0]
    years = np.array([(date - base_date).days / 365.0 for date, _ in cash_flows], dtype=float)
    amounts = np.array([amount for _, amount in cash_flows], dtype=float)

    def npv(rate: float) -> float:
        return float(np.sum(amounts / (1.0 + rate) ** years))

    # 在 [lo, hi] 内寻找变号区间
    grid = np.linspace(lo, hi, 4001)
    values = np.array([npv(r) for r in grid])
    sign_changes = np.where(np.diff(np.sign(values)) != 0)[0]
    if len(sign_changes) == 0:
        # 无变号：XIRR 不存在，返回最接近 0 的根或 0
        return 0.0

    # 取最小的正根（更符合投资语义）；若无正根则取第一个根
    best = None
    for idx in sign_changes:
        left, right = grid[idx], grid[idx + 1]
        candidate = _bisect(npv, left, right)
        if candidate > 0:
            best = candidate
            break
    if best is None:
        idx = int(sign_changes[0])
        best = _bisect(npv, grid[idx], grid[idx + 1])
    return float(best)


def _bisect(func, left: float, right: float, *, tol: float = 1e-9) -> float:
    f_left = func(left)
    for _ in range(200):
        mid = (left + right) / 2.0
        f_mid = func(mid)
        if abs(f_mid) < 1e-9:
            return mid
        if f_left * f_mid < 0:
            right = mid
        else:
            left = mid
            f_left = f_mid
    return (left + right) / 2.0


def run_backtest(
    factors_df: pd.DataFrame,
    nav_df: pd.DataFrame,
    rate_df: pd.DataFrame,
    *,
    score_column: str = "score_b",
    threshold: float = DEFAULT_THRESHOLD,
    buy_amount: float = DEFAULT_BUY_AMOUNT,
    start_cash: float = 0.0,
) -> dict:
    """运行单策略回测。

    :param factors_df: 日频因子（fund_daily_factors），需含 trade_date 与 score_column。
    :param nav_df: 净值（nav_date / unit_nav）。
    :param rate_df: cn_10y 利率（rate_date / rate_value）。
    :param score_column: 用哪一列得分判定信号（score_a / score_b）。
    :param threshold: 买入阈值（默认 80）。
    :param buy_amount: 每次买入金额（默认 6000）。
    :param start_cash: 期初闲置现金池（默认 0，即每次买入视为外部投入）。
    :return: 包含绩效指标与组合净值序列的字典。
    """
    nav = nav_df[["nav_date", "unit_nav"]].copy()
    nav["nav_date"] = pd.to_datetime(nav["nav_date"], errors="coerce")
    nav["unit_nav"] = pd.to_numeric(nav["unit_nav"], errors="coerce")
    nav = nav.dropna(subset=["nav_date", "unit_nav"]).sort_values("nav_date").drop_duplicates("nav_date")
    nav = nav.rename(columns={"nav_date": "trade_date"})

    fac = factors_df[["trade_date", score_column]].copy()
    fac["trade_date"] = pd.to_datetime(fac["trade_date"], errors="coerce")
    fac[score_column] = pd.to_numeric(fac[score_column], errors="coerce")
    fac = fac.dropna(subset=["trade_date", score_column]).sort_values("trade_date")

    rate = rate_df[["rate_date", "rate_value"]].copy()
    rate["rate_date"] = pd.to_datetime(rate["rate_date"], errors="coerce")
    rate["rate_value"] = pd.to_numeric(rate["rate_value"], errors="coerce")
    rate = rate.dropna(subset=["rate_date", "rate_value"]).sort_values("rate_date").rename(columns={"rate_date": "trade_date"})

    data = nav.merge(fac, on="trade_date", how="inner").merge(rate, on="trade_date", how="left")
    if data.empty:
        return {"xirr_pct": None, "max_drawdown_pct": None, "num_buys": 0, "total_invested": 0.0,
                "final_value": 0.0, "start_date": None, "end_date": None, "portfolio": pd.DataFrame()}
    data["rate_value"] = data["rate_value"].ffill()

    # 模型区分：start_cash > 0 → 期初现金池（买入受现金约束、期初为负现金流）；
    # start_cash == 0 → 外部逐笔投入（每次买入视为外部资金注入，不受现金池约束）。
    has_pool = start_cash > 0
    cash = float(start_cash)
    units = 0.0
    cash_flows: list[tuple[pd.Timestamp, float]] = []
    if has_pool:
        cash_flows.append((data["trade_date"].iloc[0], -start_cash))

    pv_rows: list[dict] = []
    invested = 0.0
    num_buys = 0

    for row in data.itertuples(index=False):
        trade_date = row.trade_date
        rate_value = row.rate_value
        daily_rate = (float(rate_value) / 100.0) / 365.0 if pd.notna(rate_value) else 0.0
        cash *= 1.0 + daily_rate

        score = float(row.__getattribute__(score_column))
        can_buy = (not has_pool) or cash >= buy_amount
        if score >= threshold and can_buy:
            units += buy_amount / float(row.unit_nav)
            if has_pool:
                cash -= buy_amount
            else:
                cash_flows.append((trade_date, -buy_amount))
            invested += buy_amount
            num_buys += 1

        pv_rows.append({"trade_date": trade_date, "portfolio_value": cash + units * float(row.unit_nav)})

    if not pv_rows:
        return {"xirr_pct": None, "max_drawdown_pct": None, "num_buys": 0, "total_invested": 0.0,
                "final_value": 0.0, "start_date": None, "end_date": None, "portfolio": pd.DataFrame()}

    pv_df = pd.DataFrame(pv_rows)
    final_value = float(pv_df["portfolio_value"].iloc[-1])
    cash_flows.append((pv_df["trade_date"].iloc[-1], final_value))

    xirr_pct = _xirr(cash_flows) * 100.0 if len(cash_flows) >= 2 else 0.0

    running_max = pv_df["portfolio_value"].cummax()
    drawdown = pv_df["portfolio_value"] / running_max - 1.0
    max_drawdown_pct = float(drawdown.min() * 100.0)

    return {
        "score_column": score_column,
        "threshold": threshold,
        "buy_amount": buy_amount,
        "num_buys": num_buys,
        "total_invested": invested,
        "final_value": final_value,
        "xirr_pct": xirr_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "start_date": str(data["trade_date"].min().date()),
        "end_date": str(data["trade_date"].max().date()),
        "portfolio": pv_df,
    }
