from __future__ import annotations

import sys
from pathlib import Path

import akshare as ak

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    codes = ["008163", "206018", "100018"]
    for code in codes:
        try:
            df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
            print(code, "rows=", len(df), "cols=", list(df.columns))
            print(df.head(2).to_string(index=False))
            print("---")
        except Exception as exc:
            print(code, "FAIL", type(exc).__name__, exc)


if __name__ == "__main__":
    main()