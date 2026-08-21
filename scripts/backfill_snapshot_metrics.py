"""一次性回填基金快照指标（fund_snapshot_metrics）。

用途：把 akshare 的基金规模（雪球）+ 债基持仓（东财）+ nav 派生指标
      （年化/回撤/卡玛/年限/回撤修复天数）预抓并入库。
      之后总览页固收+/债基对比表即使冷缓存也直接读库，不再每次实时调 akshare
      或重拉全历史净值。

前置：先在 Supabase SQL Editor 运行 sql/create_strategy_tables.sql
      （含 2.5 建表 + 2.6 扩展 fund_metrics/bond_metrics 列）。

用法：
  conda activate fundCraft
  $env:FUNDCRAFT_SECRET_PASSPHRASE="你的解密口令"
  python scripts/backfill_snapshot_metrics.py
"""
from pathlib import Path

from src.config import load_fund_categories, load_supabase_settings, supabase_settings_ready
from src.ui import store

ROOT = Path(__file__).resolve().parents[1]

settings = load_supabase_settings(ROOT)
assert supabase_settings_ready(settings)

categories = load_fund_categories(ROOT)
bond_plus = [str(c).zfill(6) for c in categories["固收+"].fund_codes]
bond_funds = [str(c).zfill(6) for c in categories["债基"].fund_codes]

print("== 固收+ 指标（年化/回撤/卡玛/年限/规模） ==")
for code in bond_plus:
    m = store._fund_bond_metrics(settings.url, settings.key, code)
    print(
        f"  {code}: 年化={m['annualized_return']} 回撤={m['max_drawdown']} "
        f"卡玛={m['calmar_ratio']} 年限={m['fund_age_years']} 规模={m['fund_scale']}"
    )

print("== 债基 风控指标 + 持仓 ==")
for code in bond_funds:
    rm = store._bond_risk_metrics(settings.url, settings.key, code)
    hp = store._bond_holdings_profile(settings.url, settings.key, code)
    print(
        f"  {code}: dd_all={rm['max_dd_all']} dd_1y={rm['max_dd_1y']} "
        f"recover_all_days={(rm['recover_all'] or {}).get('days')} "
        f"持仓={hp.get('categories')}"
    )

print("完成。现在重启 Streamlit 后，总览固收+/债基对比首次也会直接读库。")
