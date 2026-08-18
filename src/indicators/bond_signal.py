"""国债期货加仓信号（固收债基专用，纯派生不落库）。

规则（TF 5年期为主、T 10年期为辅；TF 期限贴合 3-5 年国开债债基）：
- 条件1·优选   TF 当日跌 ≤ -0.10%                       → 建议申购
- 条件1·强化   -0.10% < TF 跌 ≤ -0.08% 且 T 跌 ≤ -0.15%  → 建议申购
- 条件2·连跌   前 2 日债基净值均收负 且 今日盘中 TF 仍跌(<0) → 建议申购
- 其他         建议观望

历史买入点位：直接用「当日收盘」数据套同一套规则判定（与盘中 14:30 判断口径一致），
仅作历史点位标注，不构成回测/模拟收益。条件2 的「前2日」用债基净值日涨跌（落库 daily_return，%）判断。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ---- 阈值 ----
TF_PREFERRED = -0.10  # 条件1·优选：TF 单日跌幅阈值
TF_STRENGTHEN_LOW = -0.08  # 条件1·强化：TF 跌幅上界（跌得比这多且≤-0.10 已走优选）
TF_STRENGTHEN_HIGH = -0.10  # 条件1·强化：TF 跌幅下界
T_STRENGTHEN = -0.15  # 条件1·强化：T 跌幅阈值
STREAK_DAYS = 2  # 条件2：前 N 日净值收负

LEVEL_LABELS = {
    "cond1_preferred": "条件1·优选",
    "cond1_strengthen": "条件1·强化",
    "cond2_streak": "条件2·连跌",
    "none": "按兵不动",
}


def evaluate(
    tf_pct: float | None,
    t_pct: float | None,
    nav_pct_prev2: list[float] | None,
) -> dict:
    """根据当日盘中 TF/T 涨跌幅与前 N 日债基净值涨跌判定加仓信号。

    :param tf_pct: 今日盘中 TF 涨跌幅(%)；None 表示无数据。
    :param t_pct: 今日盘中 T 涨跌幅(%)；None 表示无数据。
    :param nav_pct_prev2: 当前债基前 2 个交易日净值日涨跌(%)列表；不足/无数据传 None。
    :return: {trigger, level, suggestion, reason}
    """
    if tf_pct is None:
        return {
            "trigger": False,
            "level": "none",
            "suggestion": "建议观望",
            "reason": "今日无 TF 行情数据，无法判断",
        }

    # 条件1·优选：TF 单日大跌
    if tf_pct <= TF_PREFERRED:
        return {
            "trigger": True,
            "level": "cond1_preferred",
            "suggestion": "建议申购",
            "reason": f"TF 今日跌 {tf_pct:.2f}% ≤ -0.10%，单日大跌（优选）触发",
        }

    # 条件1·强化：TF 跌幅略低于阈值，但 T 跌幅大 → 全场情绪转差
    if (TF_STRENGTHEN_HIGH < tf_pct <= TF_STRENGTHEN_LOW) and (t_pct is not None and t_pct <= T_STRENGTHEN):
        return {
            "trigger": True,
            "level": "cond1_strengthen",
            "suggestion": "建议申购",
            "reason": f"TF 跌 {tf_pct:.2f}% 且 T 跌 {t_pct:.2f}% ≤ -0.15%，情绪转差（强化）触发",
        }

    # 条件2·连跌：前 N 日债基净值均收负，且今日盘中 TF 仍跌
    if (
        nav_pct_prev2 is not None
        and len(nav_pct_prev2) >= STREAK_DAYS
        and all(value < 0 for value in nav_pct_prev2)
        and tf_pct < 0
    ):
        return {
            "trigger": True,
            "level": "cond2_streak",
            "suggestion": "建议申购",
            "reason": f"债基已连跌 {STREAK_DAYS} 日且今日盘中 TF 仍跌 {tf_pct:.2f}%，连跌触发",
        }

    return {
        "trigger": False,
        "level": "none",
        "suggestion": "建议观望",
        "reason": "未触发任何条件，今日按兵不动",
    }


def mark_buy_points(
    tf_daily: pd.DataFrame,
    t_daily: pd.DataFrame,
    nav_daily: pd.DataFrame,
    *,
    tf_preferred: float = TF_PREFERRED,
    tf_strengthen_low: float = TF_STRENGTHEN_LOW,
    tf_strengthen_high: float = TF_STRENGTHEN_HIGH,
    t_strengthen: float = T_STRENGTHEN,
) -> pd.DataFrame:
    """直接用当日收盘数据套同一套规则，标记每个交易日的买入点位（含 TF/T 日涨跌、前日净值）。

    :param tf_daily: TF 日线（trade_date, rate_value）升序。
    :param t_daily: T 日线（trade_date, rate_value）升序。
    :param nav_daily: 债基净值（nav_date, daily_return）升序；daily_return 为 %。
    :param tf_preferred: 条件1·优选 TF 跌幅阈值（%），默认 -0.10。
    :param tf_strengthen_low/tf_strengthen_high: 条件1·强化 TF 跌幅区间（%）。
    :param t_strengthen: 条件1·强化 T 跌幅阈值（%）。
    :return: DataFrame(trade_date, tf_pct, t_pct, nav_pct, level, trigger)，升序。
    """
    tf = tf_daily.copy()
    tf["trade_date"] = pd.to_datetime(tf["trade_date"], errors="coerce")
    tf = tf.dropna(subset=["trade_date", "rate_value"]).sort_values("trade_date").set_index("trade_date")
    tf["tf_pct"] = tf["rate_value"].astype(float).pct_change() * 100.0

    t = t_daily.copy()
    t["trade_date"] = pd.to_datetime(t["trade_date"], errors="coerce")
    t = t.dropna(subset=["trade_date", "rate_value"]).sort_values("trade_date").set_index("trade_date")
    t["t_pct"] = t["rate_value"].astype(float).pct_change() * 100.0

    # 以 TF 交易日为准（T 缺失那天条件1·强化不成立即可）
    frame = tf[["tf_pct"]].join(t[["t_pct"]], how="outer").sort_index()
    frame = frame[frame["tf_pct"].notna()].copy()
    if frame.empty:
        return pd.DataFrame(columns=["trade_date", "tf_pct", "t_pct", "nav_pct", "level", "trigger"])

    # 债基净值日涨跌（%）前推对齐到 TF 交易日（净值盘后公布，盘中用最近已公布值）
    nav = nav_daily.copy()
    nav["nav_date"] = pd.to_datetime(nav["nav_date"], errors="coerce")
    nav = nav.dropna(subset=["nav_date"]).sort_values("nav_date")
    if not nav.empty and "daily_return" in nav.columns:
        nav_series = nav.set_index("nav_date")["daily_return"].astype(float)
        frame["nav_pct"] = nav_series.reindex(frame.index, method="ffill")
    else:
        frame["nav_pct"] = np.nan

    prev1 = frame["nav_pct"].shift(1)
    prev2 = frame["nav_pct"].shift(2)

    cond1_pref = frame["tf_pct"] <= tf_preferred
    cond1_stre = (
        (frame["tf_pct"] > tf_strengthen_high)
        & (frame["tf_pct"] <= tf_strengthen_low)
        & (frame["t_pct"] <= t_strengthen)
    )
    cond2 = (prev1 < 0) & (prev2 < 0) & (frame["tf_pct"] < 0)

    frame["level"] = np.select(
        [cond1_pref, cond1_stre, cond2],
        ["cond1_preferred", "cond1_strengthen", "cond2_streak"],
        default="none",
    )
    frame["trigger"] = frame["level"] != "none"
    frame = frame.reset_index()
    return frame[["trade_date", "tf_pct", "t_pct", "nav_pct", "level", "trigger"]]
