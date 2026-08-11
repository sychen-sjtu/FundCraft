"""🗄️ 数据管理页：服务器数据管理与负载监控。

提供：
- 服务器负载/状态概览（负载为参考值，最近刷新来自真实同步日志）。
- 管理操作：增量刷新、强制全量刷新（对接真实同步编排 strategy_sync_runner）。
- 最近同步任务日志与各实体同步水位（真实数据）。

基金基础信息、净值明细等展示内容不放这里（属于总览 / 详情页）。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.ui import store
from src.ui.theme import render_metric_card


def _status_badge(status: str) -> str:
    status_map = {
        "success": '<span class="fc-signal-buy" style="background:#00B578;">成功</span>',
        "partial": '<span class="fc-signal-buy" style="background:#FA8C16;">部分</span>',
        "failed": '<span class="fc-signal-buy" style="background:#E64A3D;">失败</span>',
    }
    return status_map.get(status, f"<span>{status}</span>")


def _run_refresh(full: bool) -> None:
    """执行真实刷新（增量按水位补齐；全量先清水位重拉）。"""
    with st.spinner("正在同步数据并重算派生因子，请稍候..."):
        results, error = store.run_refresh(full=full)
    st.session_state["refresh_results"] = results
    st.session_state["refresh_error"] = error
    st.session_state["refresh_msg"] = f"{'全量' if full else '增量'}刷新完成"
    st.rerun()


def _render_server_status() -> None:
    st.subheader("🖥️ 存储服务器状态（Supabase）")
    status = store.get_server_status()

    c1, c2, c3 = st.columns(3)
    with c1:
        render_metric_card("数据库行数", f'{status["db_rows"]:,}')
    with c2:
        render_metric_card("数据表数", f'{status["tables"]}')
    with c3:
        render_metric_card("最近刷新", status["last_refresh"])

    # 基础设施负载：Supabase Management API 未配置前显示「暂无」（不模拟）
    c4, c5, c6 = st.columns(3)
    for col, (key, label) in zip(
        (c4, c5, c6),
        (("cpu_pct", "CPU 使用率"), ("mem_pct", "内存使用率"), ("disk_pct", "磁盘使用率")),
    ):
        with col:
            value = status.get(key)
            render_metric_card(label, f"{value}%" if value is not None else "暂无")

    st.caption(
        f'实例：{status["host"]} · 数据库行数/表数为 Supabase 真实值；'
        "CPU/内存/磁盘为 Supabase 基础设施指标，需 Management API token，暂显示「暂无」。"
    )


def _layer_status_text(status: str) -> str:
    """分层状态 → 可读文本。"""
    return {"fresh": "✅ 新鲜", "stale": "⚠️ 过期", "pending": "⬜ 暂无/待接入"}.get(status, status)


def _render_management_actions() -> None:
    st.divider()
    st.subheader("⚙️ 数据刷新")
    st.caption(
        "分层刷新：各层独立更新。增量刷新按同步水位补齐；强制全量刷新清空水位重拉。"
    )

    # ---------- 主操作 ----------
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔄 增量刷新", use_container_width=True, type="primary"):
            _run_refresh(full=False)
    with c2:
        if st.button("♻️ 强制全量刷新", use_container_width=True):
            _run_refresh(full=True)

    # ---------- 数据分层状态 ----------
    st.markdown("**数据分层状态**")
    layers = store.get_refresh_layers()
    view = pd.DataFrame(
        [
            {
                "层": f'{x["icon"]} {x["label"]}',
                "内容": x["desc"],
                "更新至": x["last_updated"],
                "状态": _layer_status_text(x["status"]),
            }
            for x in layers
        ]
    )
    st.dataframe(view, width="stretch", hide_index=True)

    # ---------- 按层刷新 ----------
    st.markdown("**按层刷新**（数据源出问题时只刷对应层）")
    cols = st.columns(len(layers))
    for col, layer in zip(cols, layers):
        with col:
            if st.button(
                f'{layer["icon"]} {layer["label"]}',
                key=f"layer_{layer['key']}",
                use_container_width=True,
            ):
                _, error = store.run_layer_refresh(layer["key"])
                st.session_state["layer_refresh_result"] = error or "完成"
                st.rerun()

    # ---------- 结果 / 消息 ----------
    error = st.session_state.get("refresh_error")
    if error:
        st.error(f"刷新失败：{error}")
    msg = st.session_state.get("refresh_msg")
    if msg:
        st.success(msg)
    layer_msg = st.session_state.get("layer_refresh_result")
    if layer_msg:
        st.info(layer_msg)

    results = st.session_state.get("refresh_results")
    if results:
        with st.expander("查看本次刷新结果"):
            for item in results:
                if "error" in item or "factor_error" in item:
                    st.error(str(item))
                else:
                    st.write(str(item))


def _render_sync_jobs() -> None:
    st.divider()
    st.subheader("🔄 最近同步任务")
    jobs = store.get_sync_jobs()
    if jobs.empty:
        st.info("暂无同步记录，请先执行一次刷新。")
        return

    view = jobs.copy()
    view["status"] = view["status"].map(_status_badge)
    view["executed_at"] = view["executed_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    view = view.rename(
        columns={
            "log_id": "ID",
            "job_name": "任务",
            "row_count": "记录数",
            "status": "状态",
            "executed_at": "执行时间",
            "message": "说明",
        }
    )
    st.markdown(
        view[["ID", "任务", "记录数", "状态", "执行时间", "说明"]].to_html(escape=False, index=False),
        unsafe_allow_html=True,
    )


def _render_watermarks() -> None:
    st.divider()
    st.subheader("🗺️ 同步水位（各实体已同步到的最大日期）")
    watermarks = store.get_watermarks()
    if watermarks.empty:
        st.info("暂无水位记录，请先执行一次刷新。")
    else:
        st.dataframe(watermarks, width="stretch", hide_index=True)


def render() -> None:
    st.title("🗄️ 数据管理")
    st.caption("服务器数据管理与状态监控（真实同步日志 / 水位 / 存储数据量）")

    if not store.is_connected():
        st.info("请先在侧边栏「数据连接」输入解密口令并连接 Supabase。")
        return

    _render_server_status()
    _render_management_actions()
    _render_sync_jobs()
    _render_watermarks()
