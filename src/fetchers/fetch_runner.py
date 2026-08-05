"""正式抓取任务编排：从 TOML 读取基金代码，抓取并落盘。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.config import load_fund_codes
from src.fetchers.akshare_fund_nav import (
    fetch_multiple_funds,
    normalize_fund_code,
    save_fund_snapshots,
)


def run_fetch_job(project_root: Path | None = None) -> Path:
    """执行一次正式抓取，返回本次快照目录。"""
    root = project_root or Path(__file__).resolve().parents[2]
    fund_codes = [normalize_fund_code(code) for code in load_fund_codes(root)]
    if not fund_codes:
        raise ValueError("未在 .streamlit/secrets.toml 中配置基金代码（[funds] fund_codes）。")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = root / "data" / "raw" / "fund_nav" / timestamp

    dataframes = fetch_multiple_funds(fund_codes)
    manifest_path = save_fund_snapshots(dataframes, output_dir)

    print(f"Saved fund snapshots to: {output_dir}")
    print(f"Manifest: {manifest_path}")
    return output_dir


if __name__ == "__main__":
    run_fetch_job()
