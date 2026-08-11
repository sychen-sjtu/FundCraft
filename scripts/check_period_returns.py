"""临时核对脚本：008163 复权净值自然月区间收益（与用户参考值核对）。"""
import calendar

import pandas as pd

from src.config import load_supabase_settings
from src.storage.supabase_store import _fetch_all_rows, create_supabase_client

s = load_supabase_settings()
c = create_supabase_client(s)
d = _fetch_all_rows(c.table("fund_nav_history").select("trade_date,adjusted_nav").eq("fund_code", "008163"))
df = pd.DataFrame(d)
df["nav_date"] = pd.to_datetime(df["trade_date"])
df = df.sort_values("nav_date").reset_index(drop=True)
latest = df["nav_date"].iloc[-1]
print("latest:", latest.date(), "adj:", df["adjusted_nav"].iloc[-1])

for label, months in [("近1月", 1), ("近3月", 3), ("近6月", 6), ("近1年", 12)]:
    y = latest.year
    m = latest.month - months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    d = min(latest.day, calendar.monthrange(y, m)[1])
    cutoff = pd.Timestamp(y, m, d)
    past = df[df["nav_date"] <= cutoff]
    base = past["adjusted_nav"].iloc[-1]
    bd = past["nav_date"].iloc[-1].date()
    ret = (df["adjusted_nav"].iloc[-1] / base - 1) * 100
    print(f"{label}: cutoff={cutoff.date()} base_date={bd} base_adj={base:.4f} ret={ret:.4f}%")
