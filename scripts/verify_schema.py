"""校验 FundCraft 数据库结构（表是否存在 / 旧命名表是否残留）。

用法（项目根目录，会提示输入解密口令，输入时不回显）：
    c:/Users/sychen/anaconda3/envs/fundCraft/python.exe scripts/verify_schema.py

比对基准：`sql/schema.sql`（唯一维护的建表脚本）+ `docs/数据库重建-数据定义.md`。
本脚本只检查「表清单」这一层；字段 / 类型 / 主键 / 外键的逐列核对，请把
`sql/schema.sql` 文末「【可选】只读校验查询」的结果贴回来，与下方 EXPECTED 对照。
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

import pandas as pd  # noqa: E402

from src.config import load_supabase_settings, supabase_settings_ready  # noqa: E402
from src.storage.supabase_store import create_supabase_client  # noqa: E402

# 期望结构：{表名: {"columns": {列: (类型, 可空)}, "pk": [列], "fk": {列: (引用表, 引用列)}}
EXPECTED: dict[str, dict] = {
    "fund_profiles": {
        "columns": {
            "fund_code": ("text", False),
            "fund_name": ("text", True),
            "fund_type": ("text", True),
            "is_etf": ("boolean", False),
            "benchmark": ("text", True),
            "source": ("text", False),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["fund_code"],
        "fk": {},
    },
    "fund_nav_history": {
        "columns": {
            "fund_code": ("text", False),
            "trade_date": ("date", False),
            "unit_nav": ("numeric", False),
            "adjusted_nav": ("numeric", True),
            "daily_return": ("numeric", True),
            "source": ("text", False),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["fund_code", "trade_date"],
        "fk": {},
    },
    "fund_dividends": {
        "columns": {
            "fund_code": ("text", False),
            "ex_date": ("date", False),
            "dividend_per_unit": ("numeric", False),
            "source": ("text", False),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["fund_code", "ex_date"],
        "fk": {},
    },
    "fund_tracking_index": {
        "columns": {
            "fund_code": ("text", False),
            "index_code": ("text", False),
            "role": ("text", False),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["fund_code", "index_code"],
        # 仅 index_code 建外键；fund_code 不建（配置先入库、档案后入库，建外键会冲突，见 sql/schema.sql）
        "fk": {"index_code": ("index_master", "index_code")},
    },
    "index_master": {
        "columns": {
            "index_code": ("text", False),
            "index_name": ("text", True),
            "index_category": ("text", False),
            "is_total_return": ("boolean", False),
            "exchange": ("text", True),
            "source": ("text", False),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["index_code"],
        "fk": {},
    },
    "index_daily_history": {
        "columns": {
            "index_code": ("text", False),
            "trade_date": ("date", False),
            "open": ("numeric", True),
            "high": ("numeric", True),
            "low": ("numeric", True),
            "close": ("numeric", False),
            "change_pct": ("numeric", True),
            "volume": ("numeric", True),
            "amount": ("numeric", True),
            "index_type": ("text", False),
            "source": ("text", False),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["index_code", "trade_date"],
        "fk": {},
    },
    "index_valuation_history": {
        "columns": {
            "index_code": ("text", False),
            "trade_date": ("date", False),
            "pe_ttm": ("numeric", True),
            "pe_lyr": ("numeric", True),
            "dividend_yield": ("numeric", True),
            "source": ("text", False),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["index_code", "trade_date"],
        "fk": {},
    },
    "macro_rates_history": {
        "columns": {
            "rate_code": ("text", False),
            "trade_date": ("date", False),
            "rate_value": ("numeric", True),
            "source": ("text", False),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["rate_code", "trade_date"],
        "fk": {},
    },
    "index_daily_factors": {
        "columns": {
            "index_code": ("text", False),
            "trade_date": ("date", False),
            "dividend_yield": ("numeric", True),
            "annualized_volatility": ("numeric", True),
            "max_drawdown": ("numeric", True),
            "dividend_yield_percentile": ("numeric", True),
            "spread": ("numeric", True),
            "spread_percentile": ("numeric", True),
            "dy_vol_ratio_percentile": ("numeric", True),
            "drawdown_percentile": ("numeric", True),
            "volatility_percentile": ("numeric", True),
            "score_a": ("numeric", True),
            "signal_a": ("boolean", True),
            "score_b": ("numeric", True),
            "signal_b": ("boolean", True),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["index_code", "trade_date"],
        "fk": {},
    },
    "sync_watermark": {
        "columns": {
            "entity_type": ("text", False),
            "entity_code": ("text", False),
            "last_date": ("date", False),
            "source": ("text", True),
            "updated_at": ("timestamp with time zone", False),
        },
        "pk": ["entity_type", "entity_code"],
        "fk": {},
    },
    "sync_job": {
        "columns": {
            "log_id": ("text", False),
            "job_name": ("text", False),
            "status": ("text", False),
            "message": ("text", True),
            "row_count": ("integer", False),
            "executed_at": ("timestamp with time zone", False),
        },
        "pk": ["log_id"],
        "fk": {},
    },
    "fund_snapshot_metrics": {
        "columns": {
            "fund_code": ("text", False),
            "fund_scale": ("numeric", True),
            "scale_updated_at": ("timestamp with time zone", True),
            "bond_report_period": ("text", True),
            "bond_categories": ("jsonb", True),
            "bond_nav_pct": ("jsonb", True),
            "bond_total_nav_pct": ("numeric", True),
            "bond_count": ("integer", True),
            "bond_no_stock": ("boolean", True),
            "bond_has_convertible": ("boolean", True),
            "holdings_updated_at": ("timestamp with time zone", True),
            "fund_metrics": ("jsonb", True),
            "fund_metrics_updated_at": ("timestamp with time zone", True),
            "bond_metrics": ("jsonb", True),
            "bond_metrics_updated_at": ("timestamp with time zone", True),
            "updated_at": ("timestamp with time zone", False),
        },
        "pk": ["fund_code"],
        "fk": {},
    },
    "fund_category": {
        "columns": {
            "category_name": ("text", False),
            "panel": ("text", False),
            "sort_order": ("integer", False),
            "created_at": ("timestamp with time zone", False),
            "updated_at": ("timestamp with time zone", False),
        },
        "pk": ["category_name"],
        "fk": {},
    },
    "fund_category_member": {
        "columns": {
            "category_name": ("text", False),
            "fund_code": ("text", False),
            "index_code": ("text", True),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["category_name", "fund_code"],
        # index_code 不加外键（先加基金后登记指数的顺序会冲突，见 sql/schema.sql）
        "fk": {"category_name": ("fund_category", "category_name")},
    },
    "ui_index_list": {
        "columns": {
            "list_key": ("text", False),
            "index_code": ("text", False),
            "sort_order": ("integer", False),
            "created_at": ("timestamp with time zone", False),
        },
        "pk": ["list_key", "index_code"],
        "fk": {},
    },
}


NEW_TABLES = list(EXPECTED.keys())
OLD_TABLES = ["fund_daily_factors", "sync_watermarks", "sync_jobs"]


def _table_count(client, name: str) -> int | None:
    """返回表行数；表不存在返回 None。"""
    try:
        resp = client.table(name).select("*", count="exact").limit(1).execute()
        return int(resp.count or 0)
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    password = getpass.getpass("请输入 secrets.toml 的解密口令：")
    settings = load_supabase_settings(root, secret_password=password or None)
    if not supabase_settings_ready(settings):
        raise SystemExit("Supabase url/key 未配置或口令错误，无法连接。")

    client = create_supabase_client(settings)

    print("\n===== 新表（重建后应全部存在） =====")
    missing: list[str] = []
    for name in NEW_TABLES:
        count = _table_count(client, name)
        if count is None:
            missing.append(name)
            print(f"  [缺失] {name}")
        else:
            print(f"  [存在] {name:28} 行数={count}")

    print("\n===== 旧表（重建后应已清理/不存在） =====")
    leftovers: list[tuple[str, int]] = []
    for name in OLD_TABLES:
        count = _table_count(client, name)
        if count is not None:
            leftovers.append((name, count))
            print(f"  [残留] {name:28} 行数={count}")
        else:
            print(f"  [已清] {name}")

    print("\n===== 汇总 =====")
    if missing:
        print(f"  ❌ 缺少新表：{missing}")
    else:
        print(f"  ✅ {len(NEW_TABLES)} 张表都存在（注意：存在不代表结构对，字段/主键/外键需再核对）")
    if leftovers:
        print(f"  ⚠️ 旧表残留：{leftovers}")
        print("     若旧表与新建表同名（如 fund_nav_history），会被 IF NOT EXISTS 跳过保持旧结构，")
        print("     必须先清理旧表再重跑建表脚本！")
    else:
        print("  ✅ 无旧命名表残留")

    print("\n字段级核对：请在 Supabase SQL Editor 运行 sql/schema.sql 文末的")
    print("「【可选】只读校验查询」（单独选中执行），把结果贴回来对照 EXPECTED。")


if __name__ == "__main__":
    main()
