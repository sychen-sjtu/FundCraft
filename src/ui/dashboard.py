"""FundCraft 主入口：全屏解锁 + 侧边导航 + 页面分发。

流程：
1. 未解锁（未连接 Supabase）时，显示全屏开屏解锁页（类似系统登录界面），
   输入解密口令后连接 Supabase 进入主界面。
2. 解锁后渲染侧边导航（总览 / 数据管理）并分发到各页面；
   侧边栏默认收起，需要时可展开。
3. 详情页不在侧边栏中，通过总览页基金卡片进入。

本文件只负责「解锁 + 导航 + 分发」，不堆业务逻辑；数据读取在 src/ui/store.py。
"""

from __future__ import annotations

import streamlit as st

from src.ui.theme import inject_global_css
from src.ui import store
from src.ui import data as data_view
from src.ui import detail as detail_view
from src.ui import home as home_view

# 页面状态键（稳定值，不含 emoji，避免字符串匹配问题）
PAGE_HOME = "overview"
PAGE_DETAIL = "detail"
PAGE_DATA = "data"

# 侧边导航项：(键, 显示标签)。
# 策略随基金绑定，在各基金详情页内展示，不放入侧边栏；详情页通过卡片点击进入。
NAV_ITEMS = [
    (PAGE_HOME, "📊 总览"),
    (PAGE_DATA, "🗄️ 数据管理"),
]
NAV_KEYS = [key for key, _ in NAV_ITEMS]
NAV_LABELS = dict(NAV_ITEMS)


def _ensure_session_state() -> None:
    # 防御：若状态被污染或不在合法页集合内，重置回总览
    if "page" not in st.session_state or st.session_state["page"] not in (*NAV_KEYS, PAGE_DETAIL):
        st.session_state["page"] = PAGE_HOME
    if "selected_fund" not in st.session_state:
        st.session_state["selected_fund"] = None


def _render_lock_screen() -> None:
    """全屏开屏解锁页：统一白色卡片（Logo/标题/身份验证/口令/按钮全部在卡片内），沿用项目浅色风格。"""
    st.markdown('<div class="lock-spacer"></div>', unsafe_allow_html=True)

    col_l, col_m, col_r = st.columns([1, 1.3, 1])
    with col_m:
        with st.container(border=True, key="lock_panel"):
            st.markdown(
                '<div class="lock-head">'
                '<div class="lock-icon">📈</div>'
                '<div class="lock-app">FundCraft</div>'
                '<div class="lock-sub">基金分析与策略看板</div>'
                "</div>",
                unsafe_allow_html=True,
            )
            st.markdown('<div class="lock-divider"></div>', unsafe_allow_html=True)
            st.markdown('<div class="lock-title">身份验证</div>', unsafe_allow_html=True)

            password = st.text_input(
                "解密口令",
                type="password",
                key="lock_password",
                label_visibility="collapsed",
                placeholder="请输入解密口令",
            )
            st.markdown('<div class="lock-gap"></div>', unsafe_allow_html=True)
            if st.button("🔓 解锁应用", type="primary", use_container_width=True, key="unlock_btn"):
                error = store.connect(password or "")
                if error:
                    st.session_state["lock_error"] = error
                else:
                    st.session_state.pop("lock_error", None)
                st.rerun()

            if st.session_state.get("lock_error"):
                st.error(st.session_state["lock_error"])


def _render_sidebar() -> None:
    with st.sidebar:
        st.title("📈 FundCraft")
        st.caption("基金分析与策略看板")
        st.divider()

        # 归一化当前页；详情页归属「总览」分组。任何非法/残留值都回退总览。
        current = st.session_state.get("page", PAGE_HOME)
        if current == PAGE_DETAIL:
            current = PAGE_HOME
        if current not in NAV_KEYS:
            current = PAGE_HOME
            st.session_state["page"] = current

        # key 用固定名 nav_radio：避免复用旧版本残留的组件状态导致 index 不匹配
        page = st.radio(
            "导航",
            NAV_KEYS,
            index=NAV_KEYS.index(current),
            format_func=lambda key: NAV_LABELS.get(key, key),
            label_visibility="collapsed",
            key="nav_radio",
        )

        # 仅当用户真正点选了某个合法导航项时才切换页面
        if page in NAV_KEYS and page != current:
            st.session_state["page"] = page
            st.rerun()

        st.divider()
        st.caption("数据源：Supabase 真实数据")
        try:
            st.caption(f"最近同步：{store.get_latest_sync_time()}")
        except Exception:  # noqa: BLE001
            pass
        if st.button("🔒 锁定 / 断开连接", use_container_width=True):
            store.disconnect()
            st.rerun()


def _scroll_to_top_on_page_change() -> None:
    """页面切换（进入/返回详情）后把滚动条拉回最上方。

    Streamlit 的实际滚动容器是内部 div（stMain），滚动 window 无效，
    因此需要先定位滚动容器再 scrollTo(0, 0)。
    """
    page = st.session_state.get("page")
    prev = st.session_state.get("_prev_page")
    if prev is not None and prev != page:
        scroll_js = (
            "<script>"
            "window.addEventListener('load',function(){"
            "var p=window.parent;"
            "var sels=['[data-testid=\"stMain\"]','[data-testid=\"stAppViewContainer\"]'];"
            "for(var i=0;i<sels.length;i++){"
            "var el=p.document.querySelector(sels[i]);"
            "if(el&&el.scrollTo){try{el.scrollTo(0,0);}catch(e){}}}"
            "try{p.scrollTo(0,0);}catch(e){}"
            "});"
            "</script>"
        )
        st.iframe(scroll_js, height=1)
    st.session_state["_prev_page"] = page


def render_dashboard() -> None:
    st.set_page_config(
        page_title="FundCraft",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    inject_global_css()
    _ensure_session_state()

    # 未解锁：显示全屏开屏界面，不渲染侧边栏与页面
    if not store.is_connected():
        _render_lock_screen()
        return

    _render_sidebar()
    _scroll_to_top_on_page_change()

    page = st.session_state["page"]

    if page == PAGE_DETAIL:
        detail_view.render()
    elif page == PAGE_DATA:
        data_view.render()
    else:
        home_view.render()

