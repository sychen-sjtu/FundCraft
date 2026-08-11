"""拉取 Supabase 全量数据到本地快照目录（新 ER 结构）。

用法（在项目根目录运行，程序会提示你输入解密口令，输入时不回显）：
    python scripts/pull_data_to_local.py

输出：data/local_snapshot/<YYYYmmdd_HHMMSS>/ 下各表 CSV：
    fund_profiles.csv / fund_nav_history.csv / fund_dividends.csv
    index_daily_history.csv / index_valuation_history.csv / index_daily_factors.csv
    macro_rates_history.csv / sync_watermark.csv / sync_job.csv

口令说明：口令用于解密 .streamlit/secrets.toml 里 [supabase] 的 url/key
（enc: 密文）。口令只在你的终端输入，不会写入任何文件。
"""

from __future__ import annotations

import getpass
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.config import (  # noqa: E402
    load_supabase_settings,
    supabase_settings_ready,
)
from src.storage.supabase_store import (  # noqa: E402
    _fetch_all_rows,
    create_supabase_client,
)


def _pull_table(client, table_name: str) -> pd.DataFrame:
    """拉取一张表的全量数据。"""
    return pd.DataFrame(_fetch_all_rows(client.table(table_name).select("*")))


def main() -> None:
    root = Path(__file__).resolve().parents[1]

    password = getpass.getpass("请输入 secrets.toml 的解密口令：")
    settings = load_supabase_settings(root, secret_password=password or None)
    if not supabase_settings_ready(settings):
        raise SystemExit("Supabase url/key 未配置或口令错误，无法连接。")

    client = create_supabase_client(settings)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = root / "data" / "local_snapshot" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []

    def _save(name: str, df: pd.DataFrame) -> None:
        df.to_csv(out_dir / f"{name}.csv", index=False, encoding="utf-8-sig")
        summary.append({"table": name, "rows": int(len(df))})
        print(f"  {name:<28} {len(df):>7} 行")

    print(f"输出目录: {out_dir}\n")

    tables = [
        ("fund_profiles", "fund_profiles"),
        ("fund_nav_history", "fund_nav_history"),
        ("fund_dividends", "fund_dividends"),
        ("index_daily_history", "index_daily_history"),
        ("index_valuation_history", "index_valuation_history"),
        ("index_daily_factors", "index_daily_factors"),
        ("macro_rates_history", "macro_rates_history"),
        ("sync_watermark", "sync_watermark"),
        ("sync_job", "sync_job"),
    ]
    for name, table in tables:
        print(f"拉取 {table} ...")
        _save(name, _pull_table(client, table))

    print("\n===== 汇总 =====")
    for row in summary:
        print(f"  {row['table']:<28} {row['rows']:>7} 行")
    print(f"\n完成，数据已保存到 {out_dir}")


if __name__ == "__main__":
    main()
