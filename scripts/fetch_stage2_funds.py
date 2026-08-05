from __future__ import annotations

from pathlib import Path
from datetime import datetime
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.fetchers.akshare_fund_nav import fetch_multiple_funds, normalize_fund_code, save_fund_snapshots


FUND_CODES = [normalize_fund_code(code) for code in ["008163", "206018", "100018"]]


def main() -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = PROJECT_ROOT / "data" / "raw" / "fund_nav" / timestamp

    dataframes = fetch_multiple_funds(FUND_CODES)
    manifest_path = save_fund_snapshots(dataframes, output_dir)

    print(f"Saved fund snapshots to: {output_dir}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()