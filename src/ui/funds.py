"""⭐ 基金配置页：网页端增删「需要关注的基金」与「市场指数条」。

设计：

- 配置存在数据库（fund_category / fund_category_member / ui_index_list，
  建表见 ``sql/schema.sql``），本页直接读写库，改完即生效。
- **运行期以库表为唯一来源**：配置表没建、或数据库不可达，就是「没有基金」；
  运行期不读任何文件配置。
- 一次性把 TOML 配置灌入库走命令行（不是页面按钮）：
  ``python scripts/check_fund_config.py --seed``
- 这里只管「关注哪些基金」；净值/分红/档案等行情数据仍需去「数据管理」页刷新一次。
"""

from __future__ import annotations

import streamlit as st

from src.storage.fund_config import PANEL_DESCRIPTIONS, PANEL_OPTIONS
from src.ui import store


_SQL_FILE = "sql/schema.sql"


def _render_setup_hint() -> None:
    """配置表未创建：给出一次性建表指引。"""
    st.error("配置表尚未创建，暂时无法在网页端管理基金。")
    st.markdown(
        "请在 Supabase 的 SQL Editor 中执行仓库里的 "
        f"`{_SQL_FILE}`（幂等脚本，可反复执行），然后刷新本页即可。"
    )


def _render_new_category() -> None:
    """新建类别表单。"""
    with st.expander("➕ 新建类别", expanded=False):
        st.caption("类别决定总览页的分组；面板决定该类别基金详情页展示哪些模块。")
        with st.form("create_category", clear_on_submit=True):
            col_name, col_panel = st.columns([2, 3])
            name = col_name.text_input("类别名称", placeholder="如 黄金 / 宽基")
            panel = col_panel.selectbox(
                "展示面板",
                PANEL_OPTIONS,
                format_func=lambda item: f"{item} —— {PANEL_DESCRIPTIONS.get(item, '')}",
            )
            submitted = st.form_submit_button("创建类别", type="primary")
        if submitted:
            if not name.strip():
                st.warning("请先填写类别名称。")
                return
            try:
                store.create_fund_category(name.strip(), panel)
            except Exception as exc:  # noqa: BLE001
                st.error(f"创建失败：{exc}")
                return
            st.success(f"已创建类别「{name.strip()}」。")
            st.rerun()


def _render_category(name: str, panel: str, members: list[dict]) -> None:
    """单个类别：成员列表（含移除）+ 添加基金表单 + 删除类别。"""
    with st.expander(f"⭐ {name} · {len(members)} 只 · {panel} 面板", expanded=False):
        if members:
            for member in members:
                code = member["fund_code"]
                index_code = member["index_code"]
                col_code, col_index, col_action = st.columns([3, 4, 2], vertical_alignment="center")
                col_code.markdown(f"**{code}**")
                col_index.caption(f"策略指数：{index_code}" if index_code else "策略指数：未设置")
                if col_action.button("🗑️ 移除", key=f"remove_{name}_{code}", use_container_width=True):
                    try:
                        store.remove_fund(name, code)
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"移除失败：{exc}")
                        return
                    st.rerun()
        else:
            st.caption("该类别下还没有基金。")

        st.divider()
        st.markdown("**添加基金**")
        with st.form(f"add_fund_{name}", clear_on_submit=True):
            col_code, col_index, col_submit = st.columns([2, 2, 1], vertical_alignment="bottom")
            fund_code = col_code.text_input("基金代码", placeholder="如 008163")
            index_code_input = col_index.text_input("策略指数（可选）", placeholder="如 H30269")
            submitted = col_submit.form_submit_button("添加", type="primary", use_container_width=True)
        if submitted:
            if not fund_code.strip():
                st.warning("请先填写基金代码。")
                return
            try:
                store.add_fund(name, fund_code.strip(), index_code_input.strip())
            except Exception as exc:  # noqa: BLE001
                st.error(f"添加失败：{exc}")
                return
            st.success(f"已把 {store.normalize_fund_code(fund_code.strip())} 加入「{name}」。")
            st.rerun()

        st.divider()
        confirmed = st.checkbox(
            "确认删除该类别（其下所有基金一并移出关注）",
            key=f"confirm_delete_{name}",
        )
        if st.button(
            "🗑️ 删除该类别",
            key=f"delete_{name}",
            disabled=not confirmed,
            use_container_width=True,
        ):
            try:
                store.delete_fund_category(name)
            except Exception as exc:  # noqa: BLE001
                st.error(f"删除失败：{exc}")
                return
            st.success(f"已删除类别「{name}」。")
            st.rerun()


def _render_market_index_section() -> None:
    """市场指数条配置（服务器管理）：增删指数 + 是否已有行情。"""
    st.subheader("📊 市场指数条")
    st.caption(
        "总览页顶部展示的指数，顺序即展示顺序。"
        "指数需在 .streamlit/secrets.toml 的 [indexes.registry] 登记并由同步任务抓取行情，"
        "否则该指数显示「暂无」。"
    )

    try:
        snapshot = store.get_market_index_snapshot()
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取市场指数配置失败：{exc}")
        return

    if not snapshot["available"]:
        st.warning("指数配置表（ui_index_list）尚未创建：请先执行 sql/schema.sql，然后刷新本页。")
        return

    for item in snapshot["items"]:
        col_name, col_state, col_action = st.columns([3, 4, 2], vertical_alignment="center")
        col_name.markdown(f"**{item['name']}**")
        state = "有行情" if item["value"] is not None else "暂无行情（未登记或未同步）"
        col_state.caption(f"{item['code']} · {state}")
        if col_action.button("🗑️ 移除", key=f"remove_index_{item['code']}", use_container_width=True):
            try:
                store.remove_market_index(item["code"])
            except Exception as exc:  # noqa: BLE001
                st.error(f"移除失败：{exc}")
                return
            st.rerun()

    if not snapshot["items"]:
        st.caption("尚未配置任何指数。")

    with st.form("add_market_index", clear_on_submit=True):
        col_code, col_submit = st.columns([4, 1], vertical_alignment="bottom")
        code = col_code.text_input("指数代码", placeholder="如 000300 / H30269")
        submitted = col_submit.form_submit_button("添加", type="primary", use_container_width=True)
    if submitted:
        if not code.strip():
            st.warning("请先填写指数代码。")
            return
        try:
            store.add_market_index(code.strip())
        except Exception as exc:  # noqa: BLE001
            st.error(f"添加失败：{exc}")
            return
        st.success(f"已添加指数 {code.strip().upper()}。")
        st.rerun()


def render() -> None:
    st.title("⭐ 基金配置")
    st.caption("网页端增删「需要关注的基金」与「市场指数条」；配置直接写服务器，保存即时生效")

    if not store.is_connected():
        st.info("请先在侧边栏「数据连接」输入解密口令并连接 Supabase。")
        return

    try:
        snapshot = store.get_fund_config_snapshot()
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取基金配置失败：{exc}")
        return

    if snapshot["source"] == "missing":
        _render_setup_hint()
        return

    st.caption(f"配置来源：服务器 · 共 {len(snapshot['categories'])} 个类别")

    _render_new_category()

    if not snapshot["categories"]:
        st.info("还没有任何类别。展开上方「➕ 新建类别」建一个，再往里面添加基金代码。")
    else:
        st.divider()
        for item in snapshot["categories"]:
            _render_category(item["name"], item["panel"], item["members"])
        st.caption("提示：新增基金后，请到「🗄️ 数据管理」执行一次基金层刷新，净值与档案才会入库。")

    st.divider()
    _render_market_index_section()
