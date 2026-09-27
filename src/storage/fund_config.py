"""基金配置（类别 + 关注列表）的表驱动读写。

职责边界：

- 数据表：``fund_category`` / ``fund_category_member``
  （建表脚本：``sql/schema.sql`` 的「五、配置域」）。
- **库表是唯一来源**：网页端增删基金直接写库，读页面也直接读库。
  TOML ``[funds.categories]`` 只在**一次性迁移**（``seed_from_toml``，CLI/脚本用）
  时作为输入；运行期完全不读它 —— 配置表未创建或数据库不可达，就是「没有基金」。
- 本模块刻意不依赖 Streamlit：UI 层（src/ui/store.py）与同步编排
  （src/storage/strategy_sync_runner.py）共用同一份解析，
  因此网页端新增的基金也会被刷新任务拉到，不需要再去改 TOML。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from src.config import DEFAULT_PANEL, FundCategory, load_fund_categories
from src.storage.supabase_store import _fetch_all_rows, normalize_fund_code


CATEGORY_TABLE = "fund_category"
MEMBER_TABLE = "fund_category_member"

# 详情页可用的展示面板（panel 决定该类别基金展示哪些模块）
PANEL_OPTIONS: tuple[str, ...] = ("净值", "固收+", "债基", "红利低波")
PANEL_DESCRIPTIONS: dict[str, str] = {
    "净值": "基础面板：业绩走势 + 净值明细 + 分红记录",
    "固收+": "基础面板 + 核心指标（历史年化/最大回撤/卡玛）",
    "债基": "基础面板 + 核心指标 + 国债期货加仓信号（不展示分红记录）",
    "红利低波": "基础面板 + RSI 动能看板",
}

# 建表脚本未执行时 PostgREST / Postgres 返回的错误特征（表不存在 → 视为没有基金）
# 注意：不能把「column xxx does not exist」也算进来 —— 那是**表结构过期**（少了个列），
# 需要原样报错让用户去跑 sql/schema.sql，而不是伪装成"没有基金"。
_MISSING_TABLE_MARKERS = (
    "could not find the table",
    "pgrst205",
    "42p01",
)

_DEFAULT_SORT_STEP = 10


def is_missing_table_error(exc: BaseException) -> bool:
    """判断异常是否为「目标表尚未创建」（而非网络/权限/表结构过期等真实故障）。

    供本模块与 src/storage/ui_index_config.py 共用（建表脚本未执行时的特征错误）。
    """
    text = str(exc).lower()
    if any(marker in text for marker in _MISSING_TABLE_MARKERS):
        return True
    # Postgres 原生写法：relation "public.xxx" does not exist
    return "relation" in text and "does not exist" in text


def fetch_fund_config(client: Any) -> dict:
    """读取库表配置。

    :return: ``{"available": bool, "categories": [...], "members": [...]}``；
             配置表不存在时 ``available=False``（调用方视为没有基金）。
    :raises: 非「表不存在」的异常原样抛出（连接/权限问题需要用户看到）。
    """
    try:
        categories = _fetch_all_rows(
            client.table(CATEGORY_TABLE).select("category_name,panel,sort_order").order("sort_order")
        )
        members = _fetch_all_rows(
            # 类别内顺序：sort_order 优先；老库补列后同为 100 时按 created_at 兜底（保持原顺序）
            client.table(MEMBER_TABLE)
            .select("category_name,fund_code,index_code,sort_order,created_at")
            .order("sort_order")
            .order("created_at")
        )
    except Exception as exc:  # noqa: BLE001
        if is_missing_table_error(exc):
            return {"available": False, "categories": [], "members": []}
        raise
    return {"available": True, "categories": categories, "members": members}


def categories_from_config(config: dict | None) -> dict[str, FundCategory]:
    """把库表配置转成 ``{类别名: FundCategory}``（保持 sort_order 顺序）。"""
    if not config or not config.get("available"):
        return {}

    ordered_names: list[str] = []
    panels: dict[str, str] = {}
    for row in config.get("categories") or []:
        name = str(row.get("category_name", "")).strip()
        if not name:
            continue
        if name not in panels:
            ordered_names.append(name)
        panels[name] = str(row.get("panel", "")).strip() or DEFAULT_PANEL

    codes: dict[str, list[str]] = {name: [] for name in ordered_names}
    index_codes: dict[str, dict[str, str]] = {name: {} for name in ordered_names}
    for row in config.get("members") or []:
        name = str(row.get("category_name", "")).strip()
        code = normalize_fund_code(row.get("fund_code", ""))
        if not name or not code:
            continue
        if name not in codes:  # 成员存在但类别行缺失（手工改库）：补一个默认类别
            ordered_names.append(name)
            panels[name] = DEFAULT_PANEL
            codes[name] = []
            index_codes[name] = {}
        if code not in codes[name]:
            codes[name].append(code)
        index_code = str(row.get("index_code") or "").strip()
        if index_code:
            index_codes[name][code] = index_code

    return {
        name: FundCategory(
            name=name,
            fund_codes=tuple(codes[name]),
            panel=panels[name],
            index_codes=index_codes[name],
        )
        for name in ordered_names
    }


def fund_codes_from_categories(categories: dict[str, FundCategory]) -> list[str]:
    """跨类别汇总基金代码（去重，保持类别顺序）。"""
    codes: list[str] = []
    for category in categories.values():
        for code in category.fund_codes:
            if code not in codes:
                codes.append(code)
    return codes


def tracking_rows_from_categories(categories: dict[str, FundCategory]) -> list[tuple[str, str, str]]:
    """跨类别汇总 ``[(fund_code, index_code, role)]``（role 固定 strategy）。"""
    rows: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for category in categories.values():
        for fund_code, index_code in category.index_codes.items():
            key = (fund_code, index_code)
            if key in seen:
                continue
            seen.add(key)
            rows.append((fund_code, index_code, "strategy"))
    return rows


def resolve_fund_categories(
    client: Any | None = None,
) -> tuple[dict[str, FundCategory], str]:
    """解析当前生效的基金类别配置（库表是唯一来源）。

    返回 ``(配置, 来源)``：

    - ``source="db"``      ：配置表可用，返回值即库表内容。**可能为空 dict** ——
      用户在网页端把类别删光了就是空。
    - ``source="missing"`` ：配置表尚未创建（还没跑 sql/schema.sql）或没有可用连接，
      视为「没有基金」，不回退任何文件配置。

    读取失败（网络 / 权限等）**不在这里兜底**，异常向上抛：由调用方决定是
    当作空（UI，见 src/ui/store.py）还是直接报错（同步任务不该静默按旧配置跑）。
    """
    if client is None:
        return {}, "missing"
    config = fetch_fund_config(client)
    if not config.get("available"):
        return {}, "missing"
    return categories_from_config(config), "db"


# ---------- 写操作（网页端「基金配置」页调用） ----------


def upsert_category(
    client: Any,
    category_name: str,
    panel: str = DEFAULT_PANEL,
    *,
    sort_order: int | None = None,
) -> None:
    """新增或更新一个基金类别（sort_order 为空时追加到末尾）。"""
    name = str(category_name).strip()
    if not name:
        raise ValueError("类别名称不能为空。")
    row: dict = {
        "category_name": name,
        "panel": str(panel).strip() or DEFAULT_PANEL,
    }
    if sort_order is not None:
        row["sort_order"] = int(sort_order)
    client.table(CATEGORY_TABLE).upsert(row, on_conflict="category_name").execute()


def delete_category(client: Any, category_name: str) -> None:
    """删除类别（成员行随外键 cascade 一起删除）。"""
    client.table(CATEGORY_TABLE).delete().eq("category_name", str(category_name).strip()).execute()


def _member_rows(client: Any, category_name: str) -> list[dict]:
    """某类别现有成员行（含 sort_order）：用于「追加到末尾」和「保持原位」。"""
    return _fetch_all_rows(
        client.table(MEMBER_TABLE)
        .select("fund_code,sort_order")
        .eq("category_name", str(category_name).strip())
    )


def _append_sort_order(rows: Iterable[dict]) -> int:
    """追加到末尾的顺序值：现有最大 sort_order + 步长。"""
    orders = [int(row["sort_order"]) for row in rows if row.get("sort_order") is not None]
    return (max(orders) if orders else 0) + _DEFAULT_SORT_STEP


def add_member(
    client: Any,
    category_name: str,
    fund_code: str,
    index_code: str | None = None,
    *,
    sort_order: int | None = None,
) -> None:
    """把一只基金加入类别。

    - 新成员：追加到类别末尾（sort_order = 现有最大值 + 步长）；
    - 已存在的成员：**保持原位置**，只更新 index_code，
      避免"只想改个指数，结果基金跳到列表末尾"。
    """
    name = str(category_name).strip()
    code = normalize_fund_code(fund_code)
    if not name or not code:
        raise ValueError("类别名称与基金代码都不能为空。")
    rows = _member_rows(client, name)
    existing = {normalize_fund_code(row.get("fund_code")): row for row in rows}
    if sort_order is None:
        current = existing.get(code, {}).get("sort_order")
        order = int(current) if current is not None else _append_sort_order(rows)
    else:
        order = int(sort_order)
    client.table(MEMBER_TABLE).upsert(
        {
            "category_name": name,
            "fund_code": code,
            "index_code": str(index_code or "").strip() or None,
            "sort_order": order,
        },
        on_conflict="category_name,fund_code",
    ).execute()


def remove_member(client: Any, category_name: str, fund_code: str) -> None:
    """把一只基金移出类别（不再关注）。"""
    (
        client.table(MEMBER_TABLE)
        .delete()
        .eq("category_name", str(category_name).strip())
        .eq("fund_code", normalize_fund_code(fund_code))
        .execute()
    )


def next_sort_order(client: Any) -> int:
    """类别追加顺序：现有最大 sort_order + 步长。"""
    rows = _fetch_all_rows(client.table(CATEGORY_TABLE).select("sort_order"))
    existing = [int(row["sort_order"]) for row in rows if row.get("sort_order") is not None]
    return (max(existing) if existing else 0) + _DEFAULT_SORT_STEP


def reorder_categories(client: Any, ordered_names: Iterable[str]) -> int:
    """按给定顺序重写类别顺序（10/20/30…），供网页端「上移 / 下移」使用。

    只写 category_name + sort_order 两列：PostgREST 的 upsert 只更新请求体里出现的列，
    panel 等其它字段保持不动。
    """
    rows = [
        {"category_name": str(name).strip(), "sort_order": (index + 1) * _DEFAULT_SORT_STEP}
        for index, name in enumerate(ordered_names)
        if str(name).strip()
    ]
    if rows:
        client.table(CATEGORY_TABLE).upsert(rows, on_conflict="category_name").execute()
    return len(rows)


def reorder_members(client: Any, category_name: str, ordered_codes: Iterable[str]) -> int:
    """按给定顺序重写某类别内基金顺序（10/20/30…），供「上移 / 下移」使用。

    同样只写顺序相关的列，index_code 不会被清空。
    """
    name = str(category_name).strip()
    rows = []
    for index, code in enumerate(ordered_codes):
        normalized = normalize_fund_code(code)
        if not normalized:
            continue
        rows.append(
            {
                "category_name": name,
                "fund_code": normalized,
                "sort_order": (index + 1) * _DEFAULT_SORT_STEP,
            }
        )
    if rows:
        client.table(MEMBER_TABLE).upsert(rows, on_conflict="category_name,fund_code").execute()
    return len(rows)


def seed_from_toml(client: Any, project_root: Path | None = None) -> dict:
    """把 TOML 配置导入库表（已存在的类别/成员保留原值，不覆盖）。

    :return: ``{"categories": 新增类别数, "members": 新增成员数, "skipped": 已有成员数}``
    """
    toml_categories = load_fund_categories(project_root)
    existing = fetch_fund_config(client)
    existing_names = {
        str(row.get("category_name", "")).strip() for row in (existing.get("categories") or [])
    }
    existing_members = {
        (str(row.get("category_name", "")).strip(), normalize_fund_code(row.get("fund_code", "")))
        for row in (existing.get("members") or [])
    }

    order = 0
    added_categories = 0
    added_members = 0
    skipped_members = 0
    for category in toml_categories.values():
        order += _DEFAULT_SORT_STEP
        if category.name not in existing_names:
            upsert_category(client, category.name, category.panel, sort_order=order)
            existing_names.add(category.name)
            added_categories += 1
        for position, code in enumerate(category.fund_codes, start=1):
            key = (category.name, code)
            if key in existing_members:
                skipped_members += 1
                continue
            add_member(
                client,
                category.name,
                code,
                category.index_codes.get(code),
                sort_order=position * _DEFAULT_SORT_STEP,
            )
            existing_members.add(key)
            added_members += 1
    return {
        "categories": added_categories,
        "members": added_members,
        "skipped": skipped_members,
    }
