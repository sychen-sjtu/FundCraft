"""查看基金档案（AkShare 拉取，只读不写库）。

用法（项目根目录）：
    c:/Users/sychen/anaconda3/envs/fundCraft/python.exe scripts/check_fund_profiles.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import pandas as pd  # noqa: E402

from src.config import load_fund_codes  # noqa: E402
from src.fetchers.akshare_fund_nav import fetch_fund_profiles, normalize_fund_code  # noqa: E402


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    codes = [normalize_fund_code(code) for code in load_fund_codes(root)]
    profiles = fetch_fund_profiles(codes)

    rows = [
        {
            "fund_code": p.get("fund_code"),
            "fund_name": p.get("fund_name"),
            "fund_type": p.get("fund_type"),
            "is_etf": p.get("is_etf"),
            "benchmark": p.get("benchmark") or p.get("tracking_index"),
        }
        for p in profiles
    ]
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
