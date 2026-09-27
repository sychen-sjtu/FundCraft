"""界面指数列表（总览页市场指数条 / 详情页对比指数下拉）的表驱动读写。

职责边界：

- 数据表：``ui_index_list``（``list_key`` + ``index_code`` + ``sort_order``），
  建表见 ``sql/schema.sql`` 的「五、配置域 5.4」。
- **库表是唯一来源**：网页端增删即生效，读取也直接读库；
  配置表未创建或数据库不可达 → 空列表（不回退任何文件配置）。
- 顺序即展示顺序（``sort_order`` 升序）。
- TOML ``[ui.market_indexes].codes`` 只在**一次性迁移**（``seed_list``，CLI 用）时作为输入。

注意：列表里的指数要能显示行情，仍需在 ``.streamlit/secrets.toml`` 的
``[indexes.registry]`` 中登记 —— 同步任务按注册表抓取指数行情/估值；
未登记的指数在指数条上显示「暂无」。把注册表也搬进库里是后续可选事项。
"""

from __future__ import annotations

from typing import Any, Iterable

from src.storage.fund_config import is_missing_table_error
from src.storage.supabase_store import _fetch_all_rows


TABLE = "ui_index_list"

# 已使用的列表键（同一张表按 list_key 区分用途）
LIST_MARKET_INDEXES = "market_indexes"   # 总览页顶部指数条
LIST_COMPARE_INDEXES = "compare_indexes"  # 预留：详情页业绩走势对比下拉

_DEFAULT_SORT_STEP = 10


def normalize_index_code(code: Any) -> str:
    """指数代码规范化：去空白 + 统一大写（H30269 / 000300S / 000001）。"""
    return str(code or "").strip().upper()


def fetch_index_list(client: Any, list_key: str) -> dict:
    """读取某用途的指数列表。

    :return: ``{"available": bool, "codes": [...]}``（按 sort_order 升序）；
             表未创建时 ``available=False``、``codes=[]``。
    :raises: 非「表不存在」的异常原样抛出（连接/权限问题需要被看到）。
    """
    key = str(list_key).strip()
    try:
        rows = _fetch_all_rows(
            client.table(TABLE).select("index_code,sort_order").eq("list_key", key).order("sort_order")
        )
    except Exception as exc:  # noqa: BLE001
        if is_missing_table_error(exc):
            return {"available": False, "codes": []}
        raise

    codes: list[str] = []
    for row in rows:
        code = normalize_index_code(row.get("index_code"))
        if code and code not in codes:
            codes.append(code)
    return {"available": True, "codes": codes}


def resolve_index_list(client: Any | None, list_key: str) -> list[str]:
    """当前生效的指数列表（库表唯一来源）。

    表未创建或没有可用连接 → 空列表；读取异常向上抛，由调用方决定
    （UI 视为空，同步任务直接报错）。
    """
    if client is None:
        return []
    return fetch_index_list(client, list_key)["codes"]


def next_sort_order(client: Any, list_key: str) -> int:
    """追加顺序：该用途现有最大 sort_order + 步长。"""
    rows = _fetch_all_rows(
        client.table(TABLE).select("sort_order").eq("list_key", str(list_key).strip())
    )
    existing = [int(row["sort_order"]) for row in rows if row.get("sort_order") is not None]
    return (max(existing) if existing else 0) + _DEFAULT_SORT_STEP


def add_index(client: Any, list_key: str, index_code: str, sort_order: int | None = None) -> None:
    """把一个指数加入列表（已存在则更新顺序）。"""
    key = str(list_key).strip()
    code = normalize_index_code(index_code)
    if not key or not code:
        raise ValueError("列表用途与指数代码都不能为空。")
    client.table(TABLE).upsert(
        {
            "list_key": key,
            "index_code": code,
            "sort_order": int(sort_order) if sort_order is not None else next_sort_order(client, key),
        },
        on_conflict="list_key,index_code",
    ).execute()


def remove_index(client: Any, list_key: str, index_code: str) -> None:
    """把一个指数移出列表。"""
    (
        client.table(TABLE)
        .delete()
        .eq("list_key", str(list_key).strip())
        .eq("index_code", normalize_index_code(index_code))
        .execute()
    )


def seed_list(client: Any, list_key: str, codes: Iterable[str]) -> dict:
    """把一组指数代码灌入列表（已存在的不覆盖）。

    :return: ``{"added": 新增数, "skipped": 已存在数}``
    """
    key = str(list_key).strip()
    existing = fetch_index_list(client, key)
    if not existing["available"]:
        raise RuntimeError(f"{TABLE} 表不存在：请先执行 sql/schema.sql")
    known = set(existing["codes"])

    added = 0
    skipped = 0
    for raw in codes:
        code = normalize_index_code(raw)
        if not code:
            continue
        if code in known:
            skipped += 1
            continue
        add_index(client, key, code)
        known.add(code)
        added += 1
    return {"added": added, "skipped": skipped}
