"""校验基金配置表读写（src/storage/fund_config.py）。

用法（项目根目录）：
    # 离线自检：用内存假客户端跑一遍读/写/删除逻辑，不需要数据库
    python scripts/check_fund_config.py

    # 连库打印当前配置（会提示输入解密口令，用于确认建表与数据是否符合预期）
    python scripts/check_fund_config.py --live

    # 一次性迁移：把 .streamlit/secrets.toml 的 [funds.categories] 灌入配置表
    # （已存在的类别/成员保留不覆盖；运行期不再读 TOML，只在需要重建配置时用一次）
    python scripts/check_fund_config.py --seed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from src.config import load_fund_categories  # noqa: E402
from src.storage.fund_config import (  # noqa: E402
    add_member,
    categories_from_config,
    delete_category,
    fetch_fund_config,
    fund_codes_from_categories,
    remove_member,
    reorder_categories,
    reorder_members,
    resolve_fund_categories,
    seed_from_toml,
    tracking_rows_from_categories,
    upsert_category,
)
from src.storage.ui_index_config import (  # noqa: E402
    LIST_MARKET_INDEXES,
    add_index,
    remove_index,
    resolve_index_list,
    seed_list,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, data: list[dict]):
        self.data = data


class FakeQuery:
    """实现 fund_config 用到的最小 Supabase 查询子集（select/order/range/eq/upsert/delete）。"""

    def __init__(self, client: "FakeClient", table: str):
        self._client = client
        self._table = table
        self._filters: list[tuple[str, object]] = []
        self._order: list[tuple[str, bool]] = []
        self._action = "select"
        self._payload: object = None
        self._on_conflict: str = ""
        self._range: tuple[int, int] | None = None

    # --- 查询构建 ---
    def select(self, _columns: str = "*") -> "FakeQuery":
        self._action = "select"
        return self

    def order(self, column: str, desc: bool = False) -> "FakeQuery":
        self._order.append((column, desc))
        return self

    def range(self, start: int, end: int) -> "FakeQuery":
        self._range = (start, end)
        return self

    def eq(self, column: str, value: object) -> "FakeQuery":
        self._filters.append((column, value))
        return self

    def neq(self, _column: str, _value: object) -> "FakeQuery":
        return self

    # --- 写操作 ---
    def upsert(self, payload, on_conflict: str | None = None) -> "FakeQuery":
        self._action = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict or ""
        return self

    def delete(self) -> "FakeQuery":
        self._action = "delete"
        return self

    def execute(self) -> FakeResponse:
        if self._client.missing_tables:
            raise Exception(
                f"Could not find the table 'public.{self._table}' in the schema cache (PGRST205)"
            )

        rows = self._client.tables.setdefault(self._table, [])

        if self._action == "select":
            out = [dict(row) for row in rows]
            for column, value in self._filters:
                out = [row for row in out if row.get(column) == value]
            # 与 PostgREST 一致：多次 order 依次生效（先按第一个键排，再按第二个）
            for column, desc in reversed(self._order):
                out.sort(key=lambda row: (row.get(column) is None, row.get(column)), reverse=desc)
            if self._range is not None:
                start, end = self._range
                out = out[start : end + 1]
            return FakeResponse(out)

        if self._action == "delete":
            removed_categories = [
                row["category_name"]
                for row in rows
                if self._table == "fund_category"
                and all(row.get(column) == value for column, value in self._filters)
            ]
            rows[:] = [
                row
                for row in rows
                if not all(row.get(column) == value for column, value in self._filters)
            ]
            if self._table == "fund_category":
                members = self._client.tables.setdefault("fund_category_member", [])
                members[:] = [
                    row for row in members if row.get("category_name") not in removed_categories
                ]
            return FakeResponse([])

        items = self._payload if isinstance(self._payload, list) else [self._payload]
        keys = [key.strip() for key in self._on_conflict.split(",") if key.strip()]
        for item in items:
            item = dict(item)
            existing = None
            if keys:
                existing = next(
                    (row for row in rows if all(row.get(key) == item.get(key) for key in keys)),
                    None,
                )
            if existing is not None:
                existing.update(item)
            else:
                rows.append(item)
        return FakeResponse([])


class FakeClient:
    def __init__(self, *, missing_tables: bool = False):
        self.tables: dict[str, list[dict]] = {
            "fund_category": [],
            "fund_category_member": [],
            "ui_index_list": [],
        }
        self.missing_tables = missing_tables

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)


def _check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail and not condition else ""))
    return condition


def run_offline_checks() -> bool:
    """离线自检：空表 / 未建表 / 导入 / 增删 / 级联删除。"""
    toml_categories = load_fund_categories(ROOT)
    toml_fund_codes = fund_codes_from_categories(toml_categories)
    ok = True

    # 1) 未建表 → 视为没有基金（不读任何文件配置）
    categories, source = resolve_fund_categories(FakeClient(missing_tables=True))
    ok &= _check("未建表时配置为空（source=missing）", source == "missing" and categories == {})

    # 2) 表存在但为空 → 来源就是库表、配置为空（网页端删光就是空）
    empty_client = FakeClient()
    config = fetch_fund_config(empty_client)
    ok &= _check("空表 available=True 且无类别", config["available"] and categories_from_config(config) == {})
    categories, source = resolve_fund_categories(empty_client)
    ok &= _check("空表时来源为 db 且配置为空", source == "db" and categories == {})

    # 2b) 没有可用连接 → 同样视为没有基金
    categories, source = resolve_fund_categories(None)
    ok &= _check("无连接时配置为空", source == "missing" and categories == {})

    # 3) 导入 TOML → 类别/成员数量与 TOML 一致
    client = FakeClient()
    first = seed_from_toml(client, ROOT)
    ok &= _check(
        "导入类别数一致",
        first["categories"] == len(toml_categories),
        f"expected {len(toml_categories)}, got {first['categories']}",
    )
    ok &= _check(
        "导入成员数一致",
        first["members"] == len(toml_fund_codes),
        f"expected {len(toml_fund_codes)}, got {first['members']}",
    )

    # 4) 重复导入幂等（不覆盖、不重复）
    second = seed_from_toml(client, ROOT)
    ok &= _check("重复导入新增为 0", second["categories"] == 0 and second["members"] == 0)
    ok &= _check("重复导入全部跳过", second["skipped"] == len(toml_fund_codes))

    # 5) 导入后解析出来的配置与 TOML 等价（名称/面板/代码/指数映射）
    categories, source = resolve_fund_categories(client)
    same_names = list(categories.keys()) == list(toml_categories.keys())
    same_panels = all(categories[key].panel == toml_categories[key].panel for key in toml_categories)
    same_codes = all(
        set(categories[key].fund_codes) == set(toml_categories[key].fund_codes) for key in toml_categories
    )
    ok &= _check("库表配置与 TOML 等价", source == "db" and same_names and same_panels and same_codes)
    ok &= _check(
        "指数映射一致",
        set(tracking_rows_from_categories(categories)) == set(tracking_rows_from_categories(toml_categories)),
    )

    # 6) 增删基金
    target = next(iter(categories))
    add_member(client, target, "159915", "399006")
    categories = categories_from_config(fetch_fund_config(client))
    ok &= _check("新增基金生效", "159915" in categories[target].fund_codes)
    ok &= _check("新增基金的指数映射生效", categories[target].index_codes.get("159915") == "399006")
    remove_member(client, target, "159915")
    categories = categories_from_config(fetch_fund_config(client))
    ok &= _check("移除基金生效", "159915" not in categories[target].fund_codes)

    # 7) 新建类别 + 级联删除
    upsert_category(client, "测试类别", "净值", sort_order=999)
    add_member(client, "测试类别", "000001")
    categories = categories_from_config(fetch_fund_config(client))
    ok &= _check("新建类别并加基金", categories.get("测试类别") is not None and categories["测试类别"].fund_codes == ("000001",))
    delete_category(client, "测试类别")
    config = fetch_fund_config(client)
    ok &= _check(
        "删除类别级联删除成员",
        not any(row["category_name"] == "测试类别" for row in config["categories"])
        and not any(row["category_name"] == "测试类别" for row in config["members"]),
    )

    # 8) 界面指数列表（市场指数条）：未建表为空、增删、seed 只增不覆盖
    ok &= _check(
        "指数列表未建表时为空",
        resolve_index_list(FakeClient(missing_tables=True), LIST_MARKET_INDEXES) == [],
    )
    index_client = FakeClient()
    ok &= _check("指数列表空表时为空", resolve_index_list(index_client, LIST_MARKET_INDEXES) == [])
    add_index(index_client, LIST_MARKET_INDEXES, "000001")
    add_index(index_client, LIST_MARKET_INDEXES, "h30269")  # 小写应被规范化
    codes = resolve_index_list(index_client, LIST_MARKET_INDEXES)
    ok &= _check("指数加入并规范化大小写", codes == ["000001", "H30269"], codes)
    remove_index(index_client, LIST_MARKET_INDEXES, "000001")
    ok &= _check("指数移除生效", resolve_index_list(index_client, LIST_MARKET_INDEXES) == ["H30269"])
    seeded = seed_list(index_client, LIST_MARKET_INDEXES, ["000300", "H30269"])
    ok &= _check("指数 seed 只增不覆盖", seeded == {"added": 1, "skipped": 1}, seeded)

    # 9) 顺序调整：类别排序 / 类别内基金排序（顺序即展示顺序）
    order_client = FakeClient()
    upsert_category(order_client, "A", "净值", sort_order=10)
    upsert_category(order_client, "B", "净值", sort_order=20)
    add_member(order_client, "B", "000001", "X1")
    add_member(order_client, "B", "000002")
    cats = categories_from_config(fetch_fund_config(order_client))
    ok &= _check(
        "初始顺序：类别 A,B / 类别内 000001,000002",
        list(cats) == ["A", "B"] and cats["B"].fund_codes == ("000001", "000002"),
        f"{list(cats)} {cats['B'].fund_codes}",
    )
    reorder_categories(order_client, ["B", "A"])
    ok &= _check(
        "类别重排生效",
        list(categories_from_config(fetch_fund_config(order_client))) == ["B", "A"],
    )
    reorder_members(order_client, "B", ["000002", "000001"])
    cats = categories_from_config(fetch_fund_config(order_client))
    ok &= _check("类别内重排生效", cats["B"].fund_codes == ("000002", "000001"), cats["B"].fund_codes)
    ok &= _check("重排后 index_code 未丢", cats["B"].index_codes.get("000001") == "X1")
    add_member(order_client, "B", "000001", "X9")  # 已存在：应保持原位、只更新指数
    cats = categories_from_config(fetch_fund_config(order_client))
    ok &= _check(
        "重复添加已存在的基金会保持原位",
        cats["B"].fund_codes == ("000002", "000001") and cats["B"].index_codes.get("000001") == "X9",
        f"{cats['B'].fund_codes} {cats['B'].index_codes}",
    )
    return ok


def run_live_check() -> int:
    """连库读取当前配置并打印（只读，不写入）。"""
    from src.config import load_supabase_settings, supabase_settings_ready
    from src.storage.supabase_store import create_supabase_client

    settings = load_supabase_settings(ROOT)
    if not supabase_settings_ready(settings):
        print("Supabase 配置不完整（缺少 url/key），无法连库。")
        return 1
    client = create_supabase_client(settings)
    config = fetch_fund_config(client)
    if not config["available"]:
        print("配置表不存在：请先在 Supabase SQL Editor 执行 sql/schema.sql（唯一建表脚本）")
        return 1
    categories = categories_from_config(config)
    if not categories:
        print("配置表存在但为空：可执行 python scripts/check_fund_config.py --seed 把 TOML 配置灌入。")
        return 0
    print(f"配置表共 {len(categories)} 个类别：")
    for name, category in categories.items():
        print(f"  · {name}（面板 {category.panel}，{len(category.fund_codes)} 只）")
        for code in category.fund_codes:
            index_code = category.index_codes.get(code, "")
            print(f"      {code}" + (f"  ← {index_code}" if index_code else ""))
    return 0


def run_seed() -> int:
    """一次性迁移：把 .streamlit/secrets.toml 的 [funds.categories] 灌入配置表（只增不覆盖）。"""
    from src.config import load_supabase_settings, supabase_settings_ready
    from src.storage.supabase_store import create_supabase_client

    settings = load_supabase_settings(ROOT)
    if not supabase_settings_ready(settings):
        print("Supabase 配置不完整（缺少 url/key），无法连库。")
        return 1
    client = create_supabase_client(settings)
    config = fetch_fund_config(client)
    if not config["available"]:
        print("配置表不存在：请先在 Supabase SQL Editor 执行 sql/schema.sql")
        return 1
    result = seed_from_toml(client, ROOT)
    print(
        f"导入完成：新增类别 {result['categories']} 个、基金 {result['members']} 只"
        f"（已存在 {result['skipped']} 只，未覆盖）。"
    )

    # 市场指数条：[ui.market_indexes].codes → ui_index_list
    from src.config import load_market_index_codes

    try:
        index_result = seed_list(client, LIST_MARKET_INDEXES, load_market_index_codes(ROOT))
        print(
            f"市场指数条：新增 {index_result['added']} 个指数、已存在 {index_result['skipped']} 个"
            "（未覆盖）。"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"市场指数条导入失败（ui_index_list 表可能还没建）：{exc}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="校验基金配置表读写")
    parser.add_argument("--live", action="store_true", help="连库打印当前配置（只读）")
    parser.add_argument("--seed", action="store_true", help="把 .streamlit/secrets.toml 的配置一次性灌入配置表")
    args = parser.parse_args()

    if args.seed:
        return run_seed()
    if args.live:
        return run_live_check()

    ok = run_offline_checks()
    print("\n离线自检：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
