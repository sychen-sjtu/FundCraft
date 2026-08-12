"""基金详情页：大净值头部 + 折线图行情 + 策略指标（默认不计算，按钮触发）+ 分红。

设计原则：
- 全部用折线图展示，不使用表格。
- 「策略指标」（即策略因子）默认不计算，点击「计算策略指标」后展示当日指标 + 变化趋势折线图。
- 净值走势 / 最大回撤 / 分红记录 默认折叠。
- 「红利低波」类基金展示该基金自己的策略指标。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.config import load_compare_index_codes, load_index_registry
from src.ui import store
from src.ui.charts import (
    build_cumulative_vs_benchmark_chart,
    build_dividend_history_chart,
    build_drawdown_area_chart,
    build_nav_area_chart,
    build_performance_chart,
    build_rsi_dashboard_chart,
    build_strategy_scores_chart,
)
from src.ui.theme import COLOR_BENCHMARK, COLOR_FUND_HIGHLIGHT, PLOTLY_CONFIG, detail_head_html


def _render_back_and_header(code: str) -> None:
    meta = store.get_fund_meta(code)
    latest = store.get_latest_nav(code)

    col_back, col_head = st.columns([1, 5])
    with col_back:
        if st.button("← 返回", use_container_width=True):
            st.session_state["page"] = "overview"
            st.session_state.pop("selected_fund", None)
            st.rerun()
    with col_head:
        nav_date = pd.Timestamp(latest["nav_date"]).strftime("%Y-%m-%d")
        st.markdown(
            detail_head_html(
                fund_name=meta["fund_name"],
                fund_code=code,
                category=meta["category"],
                latest_nav=latest["unit_nav"],
                daily_change=latest["daily_return_pct"],
                nav_date=f"净值日期：{nav_date}",
            ),
            unsafe_allow_html=True,
        )


def _render_evaluation(code: str) -> None:
    """🔍 基金评估：渐进式三层就绪度（小字一行说明即可，不占大版面；绝不模拟/估算）。"""
    st.divider()
    st.subheader("🔍 基金评估")
    ev = store.get_fund_evaluation(code)

    status = ev.get("status", "blocked")
    badge = {"ok": "🟢 完整", "partial": "🟡 部分", "blocked": "🔴 暂无"}.get(status, status)
    missing = ev.get("missing", [])
    missing_txt = "、".join(missing) if missing else "无"
    hint = ev.get("hint", "") or "—"

    layers = ev.get("layers", {})
    parts = []
    for key, label in [("fund", "基金"), ("index", "指数"), ("strategy", "策略")]:
        layer = layers.get(key)
        if layer and layer.get("available"):
            detail_text = _layer_detail_text(key, layer)
            parts.append(f"<b>{label}</b>✅<span style='color:#B0B6BF'>（{detail_text}）</span>")
        else:
            parts.append(f"<b>{label}</b>⬜")

    st.markdown(
        f'<div style="font-size:12px;color:#8A8F99;line-height:1.9;">'
        f"状态：{badge} &nbsp;|&nbsp; 缺失：{missing_txt} &nbsp;|&nbsp; 提示：{hint}<br>"
        f"数据分层：{' &nbsp; '.join(parts)}</div>",
        unsafe_allow_html=True,
    )


def _layer_detail_text(key: str, layer: dict) -> str:
    """分层就绪度的可读摘要（全部来自真实数据量，不模拟）。"""
    if key == "fund":
        return (
            f"净值 {layer.get('nav_rows', 0)} 条 · 分红 {layer.get('dividend_rows', 0)} 条"
            f" · {layer.get('start_date', '?')} ~ {layer.get('end_date', '?')}"
        )
    if key == "index":
        return (
            f"指数行情 {layer.get('price_rows', 0)} 条"
            f" · {layer.get('start_date', '?')} ~ {layer.get('end_date', '?')}"
        )
    if key == "strategy":
        return (
            f"指数 {layer.get('price_rows', 0)} 条 · 股息率 {layer.get('dy_rows', 0)} 条"
            f" · PE {layer.get('pe_rows', 0)} 条 · 利率 {layer.get('rate_rows', 0)} 条"
        )
    return ""


_INDEX_NAME_FALLBACK = {
    "000300S": "沪深300全收益",
    "000300": "沪深300",
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
}


@st.cache_data(ttl=300, show_spinner=False)
def _compare_index_options() -> list[dict]:
    """业绩走势可选对比指数（配置驱动：TOML [ui.compare_indexes].codes，名称取注册表）。"""
    registry = load_index_registry(store.PROJECT_ROOT)
    result: list[dict] = []
    for code in load_compare_index_codes(store.PROJECT_ROOT):
        spec = registry.get(code)
        name = (spec.index_name if spec and spec.index_name else "") or _INDEX_NAME_FALLBACK.get(code, code)
        result.append({"code": code, "name": name})
    return result


def _latest_cum_return(frame: pd.DataFrame, col: str | None = None) -> float | None:
    """区间累计收益率（%）：基金用复权净值（回退单位净值），对比基准用 benchmark 列。"""
    if frame is None or frame.empty:
        return None
    ordered = frame.sort_values("nav_date").reset_index(drop=True)
    if col is None:
        col = "adjusted_nav" if "adjusted_nav" in ordered.columns and ordered["adjusted_nav"].notna().any() else "unit_nav"
    if col not in ordered.columns:
        return None
    base = float(ordered[col].iloc[0])
    last = float(ordered[col].iloc[-1])
    return (last / base - 1.0) * 100.0 if base else None


def _pct_cls(value) -> str:
    if value is None:
        return "flat"
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def _render_legend_stats(items: list[dict]) -> None:
    """图例 + 最新涨跌幅统计条（折线图正上方）。items: [{label, color, value, cls, w, h}]。

    图标为小横线（主线粗、对比线细），数值颜色严格语义化（红涨绿跌、保留 +/-）。
    """
    html = '<div class="fc-legend-strip">' + "".join(
        f'<div class="fc-legend-item">'
        f'<span class="fc-legend-swatch" style="background:{it["color"]};width:{it.get("w", 20)}px;height:{it.get("h", 3)}px;"></span>'
        f'<span>{it["label"]}</span>'
        f'<span class="fc-legend-value {it["cls"]}">{it["value"]}</span>'
        f"</div>"
        for it in items
    ) + "</div>"
    st.markdown(html, unsafe_allow_html=True)


def _render_performance_chart(code: str, nav_df: pd.DataFrame, range_key: str) -> None:
    """业绩走势：复权净值累计收益率折线图，可下拉选择对比大盘指数（TOML 配置）。"""
    options = _compare_index_options()
    codes = [opt["code"] for opt in options]
    names = {opt["code"]: opt["name"] for opt in options}

    # 单行头部：业绩走势(小字) + “？”帮助气泡 + 对比指数下拉
    # 用横向容器：桌面同一行、窄屏（手机）自动换行，电脑/手机兼容
    with st.container(horizontal=True, key="perf_head"):
        st.markdown(
            '<span style="font-size:13px;font-weight:700;color:#1F2329;">业绩走势'
            '<span class="fc-help" data-tip="复权净值累计收益率：分红基金用复权净值（红利再投资口径），区间首日归一化为 0%，随时间范围胶囊联动。">?</span>'
            '</span>',
            unsafe_allow_html=True,
        )
        selected = st.selectbox(
            "对比指数",
            options=[""] + codes,
            format_func=lambda c: "— 不对比 —" if not c else names.get(c, c),
            index=0,
            label_visibility="collapsed",
            placeholder="对比指数",
            key="compare_index_select",
        )

    fund_ret = _latest_cum_return(nav_df)
    if selected:
        bench = store.get_index_benchmark(selected, range_key=range_key)
        bench_name = names.get(selected, selected)
        bench_ret = _latest_cum_return(bench, col="benchmark") if not bench.empty else None
        fig = build_cumulative_vs_benchmark_chart(nav_df, bench, benchmark_name=bench_name, show_legend=False)
        items = [
            {"label": "本基金", "color": COLOR_FUND_HIGHLIGHT, "value": _fmt_pct(fund_ret), "cls": _pct_cls(fund_ret), "w": 20, "h": 3},
            {"label": bench_name, "color": COLOR_BENCHMARK, "value": _fmt_pct(bench_ret), "cls": _pct_cls(bench_ret), "w": 20, "h": 1},
        ]
    else:
        fig = build_performance_chart(nav_df, show_legend=False)
        items = [
            {"label": "本基金", "color": COLOR_FUND_HIGHLIGHT, "value": _fmt_pct(fund_ret), "cls": _pct_cls(fund_ret), "w": 20, "h": 3},
        ]
    _render_legend_stats(items)
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG, key="perf_chart")


def _render_nav_table(code: str, range_key: str) -> None:
    """历史净值明细：轻量无边框表格（单位净值/累计净值/日涨跌幅，最新在前）。"""
    nav = store.get_nav_history_with_cumulative(code, range_key=range_key)
    if nav.empty:
        st.markdown(
            '<div class="fc-panel-head"><span class="fc-panel-title">历史净值明细</span>'
            '<span class="fc-panel-note">暂无净值数据</span></div>',
            unsafe_allow_html=True,
        )
        return
    view = nav.sort_values("nav_date", ascending=False).reset_index(drop=True)
    rows_html = []
    for row in view.itertuples(index=False):
        date = pd.Timestamp(row.nav_date).strftime("%Y-%m-%d")
        unit = f"{float(row.unit_nav):.4f}" if pd.notna(row.unit_nav) else "—"
        cum = f"{float(row.cumulative_nav):.4f}" if pd.notna(row.cumulative_nav) else "—"
        dr = row.daily_return
        if pd.isna(dr):
            dr_html = '<span class="fc-flat fc-num">—</span>'
        else:
            dr = float(dr)
            cls = "fc-up" if dr > 0 else ("fc-down" if dr < 0 else "fc-flat")
            sign = "+" if dr > 0 else ""
            dr_html = f'<span class="{cls} fc-num">{sign}{dr:.2f}%</span>'
        rows_html.append(
            f"<tr><td>{date}</td><td class='num'>{unit}</td><td class='num'>{cum}</td>"
            f"<td class='num'>{dr_html}</td></tr>"
        )
    st.markdown(
        '<div class="fc-panel-head"><span class="fc-panel-title">历史净值明细</span>'
        '<span class="fc-panel-note">最新在前</span></div>'
        '<div class="fc-nav-table-wrap"><table class="fc-nav-table">'
        "<thead><tr><th>日期</th><th class='num'>单位净值</th><th class='num'>累计净值</th>"
        "<th class='num'>日涨跌幅</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></div>",
        unsafe_allow_html=True,
    )


def _render_strategy_signal(code: str) -> None:
    """策略指标：默认不计算，点按钮后计算并突出显示当日策略指标 + 变化趋势折线图。"""
    st.divider()
    st.subheader("🎯 策略指标")

    computed_key = f"strategy_computed_{code}"
    if not st.session_state.get(computed_key, False):
        st.caption("策略指标（策略因子）默认不计算，计算较耗时。点击下方按钮开始计算，计算后展示当日策略指标与变化趋势。")
        if st.button("🧮 计算策略指标", type="primary"):
            st.session_state[computed_key] = True
            st.rerun()
        return

    overview = store.get_strategy_overview(code)
    if not overview:
        st.info("暂无策略因子数据，请先在数据管理页执行刷新。")
        return

    st.markdown(
        f'<span class="fc-today-tag">今日</span><b>当日策略指标</b>'
        f'<span style="font-size:13px;color:#8A8F99;margin-left:8px;">信号日期：{pd.Timestamp(overview["trade_date"]).strftime("%Y-%m-%d")}</span>',
        unsafe_allow_html=True,
    )

    signal_a = overview["signal_a"]
    signal_b = overview["signal_b"]

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        badge_a = '<span class="fc-signal-buy">买入</span>' if signal_a else '<span class="fc-signal-wait">观望</span>'
        st.markdown(
            f'<div class="fc-metric-today"><div class="fc-metric-label">A 策略得分</div>'
            f'<div class="fc-metric-value">{overview["score_a"]:.1f}</div>'
            f'<div style="margin-top:6px;">{badge_a}</div></div>',
            unsafe_allow_html=True,
        )
    with c2:
        badge_b = '<span class="fc-signal-buy">买入</span>' if signal_b else '<span class="fc-signal-wait">观望</span>'
        st.markdown(
            f'<div class="fc-metric-today"><div class="fc-metric-label">B 策略得分</div>'
            f'<div class="fc-metric-value">{overview["score_b"]:.1f}</div>'
            f'<div style="margin-top:6px;">{badge_b}</div></div>',
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f'<div class="fc-metric-today"><div class="fc-metric-label">指数股息率</div>'
            f'<div class="fc-metric-value">{overview["dividend_yield"]:.2f}%</div></div>',
            unsafe_allow_html=True,
        )
    with c4:
        st.markdown(
            f'<div class="fc-metric-today"><div class="fc-metric-label">股息率-10Y 利差</div>'
            f'<div class="fc-metric-value">{overview["spread"]:.2f}%</div></div>',
            unsafe_allow_html=True,
        )

    # 策略指标变化趋势折线图（A/B 得分）
    factors = store.get_strategy_factors(code, tail=180)
    if not factors.empty:
        st.markdown("**策略指标变化趋势（A/B 得分）**")
        st.plotly_chart(build_strategy_scores_chart(factors), width="stretch", config=PLOTLY_CONFIG, key="strategy_scores")

    # ---------- 策略回测占位（字段保留，功能未接入：真实回测引擎见 strategy_backtest.py） ----------
    st.divider()
    st.subheader("🧪 策略回测")
    backtest_key = f"strategy_backtest_{code}"
    if not st.session_state.get(backtest_key, False):
        st.caption("策略回测功能暂未接入，字段先保留。")
        if st.button("🧪 计算策略回测"):
            st.session_state[backtest_key] = True
            st.rerun()
        return

    bt = store.get_backtest_overview()
    if bt:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("XIRR 年化", f'{bt["xirr_pct"]:.1f}%')
        c2.metric("组合最大回撤", f'{bt["max_drawdown_pct"]:.1f}%')
        c3.metric("信号触发", f'{bt["buy_count"]} 次')
        c4.metric("回测区间", str(bt["period"]))
    else:
        st.info("暂无回测数据（回测功能待接入，不提供模拟数值）。")


# RSI 看板信号类型 → 中文标签 / 颜色（与 charts._RSI_COLORS 一致）
_RSI_KIND_LABEL = {"A": "A 共振低吸", "B": "B 底背离", "C": "C 钝化警示"}
_RSI_KIND_COLOR = {"A": "#00B578", "B": "#722ED1", "C": "#FA8C16"}
_RSI_LOW, _RSI_HIGH = 35.0, 65.0


def _rsi_zone(value) -> tuple[str, str]:
    """RSI 分区：(文案, 颜色)。<35 低吸(绿) / 35~65 中性(灰) / >65 风险(红)。"""
    if value is None or pd.isna(value):
        return "—", "#8A8F99"
    if value < _RSI_LOW:
        return "低吸", "#00B578"
    if value > _RSI_HIGH:
        return "风险", "#E64A3D"
    return "中性", "#8A8F99"


def _rsi_kpi_html(label: str, value_text: str, zone_label: str, zone_color: str) -> str:
    """RSI 当日指标小卡（复用 .fc-metric-today 白卡样式，底部带分区标签）。"""
    return (
        f'<div class="fc-metric-today">'
        f'<div class="fc-metric-label">{label}</div>'
        f'<div class="fc-metric-value">{value_text}</div>'
        f'<div style="font-size:12px;margin-top:2px;color:{zone_color};font-weight:600;">{zone_label}</div>'
        f"</div>"
    )


def _fwd_cell(value) -> str:
    """前瞻收益单元格（遵循用户口径：>0 绿 / <0 红；与图3柱一致）。"""
    if value is None or pd.isna(value):
        return '<span class="fc-flat">—</span>'
    color = "#00B578" if value > 0 else ("#E64A3D" if value < 0 else "#8A8F99")
    sign = "+" if value > 0 else ""
    return f'<span style="color:{color};font-weight:600;">{sign}{value:.1f}%</span>'


def _render_rsi_signals_table(signals: pd.DataFrame) -> None:
    """信号明细轻量表（最新在前，最多 12 条）：日期/模式/周RSI/日RSI/前瞻收益/60日回撤。"""
    view = signals.sort_values("nav_date", ascending=False).head(12).reset_index(drop=True)
    rows_html = []
    for row in view.itertuples(index=False):
        date = pd.Timestamp(row.nav_date).strftime("%Y-%m-%d")
        kind = str(row.kind)
        label = _RSI_KIND_LABEL.get(kind, kind)
        color = _RSI_KIND_COLOR.get(kind, "#1F2329")
        w_rsi = f"{float(row.weekly_rsi):.1f}" if pd.notna(row.weekly_rsi) else "—"
        d_rsi = f"{float(row.daily_rsi):.1f}" if pd.notna(row.daily_rsi) else "—"
        rows_html.append(
            f"<tr><td>{date}</td>"
            f"<td><span style='color:{color};font-weight:600;'>{label}</span></td>"
            f"<td class='num'>{w_rsi}</td><td class='num'>{d_rsi}</td>"
            f"<td class='num'>{_fwd_cell(getattr(row, 'fwd_20', None))}</td>"
            f"<td class='num'>{_fwd_cell(getattr(row, 'fwd_60', None))}</td>"
            f"<td class='num'>{_fwd_cell(getattr(row, 'fwd_120', None))}</td>"
            f"<td class='num'>{_fwd_cell(getattr(row, 'mdd60', None))}</td>"
            f"</tr>"
        )
    st.markdown(
        '<div class="fc-panel-head"><span class="fc-panel-title">RSI 历史信号</span>'
        '<span class="fc-panel-note">最新在前 · 前瞻收益=信号后交易日累计收益 · 回撤=未来60日最大回撤</span></div>'
        '<div class="fc-nav-table-wrap"><table class="fc-nav-table">'
        "<thead><tr><th>日期</th><th>模式</th><th class='num'>周RSI</th><th class='num'>日RSI</th>"
        "<th class='num'>T+20</th><th class='num'>T+60</th><th class='num'>T+120</th>"
        "<th class='num'>60日回撤</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></div>",
        unsafe_allow_html=True,
    )


def _render_rsi_dashboard(code: str) -> None:
    """RSI 动能看板（红利低波专用）：三层联动图 + 当日指标卡 + 信号明细。

    全部由真实复权净值派生（派生不落库）；股息率利差仅在有策略指数映射的基金显示
    （007466→H30269 有，008163 无映射 → 该卡/线显示「暂无」，绝不模拟）。
    """
    st.divider()
    with st.container(horizontal=True, key=f"rsi_head_{code}"):
        st.markdown(
            '<span style="font-size:13px;font-weight:700;color:#1F2329;">📊 RSI 动能看板'
            '<span class="fc-help" data-tip="日RSI=复权净值 Wilder 平滑(6/12)；周RSI=周收盘(12/24)；'
            '图1复权净值+250MA+历史信号，图2周/日RSI+30/35/65/70参考线（<35浅绿低吸区、>65浅红风险区）。'
            '模式A共振低吸/模式B底背离/模式C钝化陷阱，前瞻收益见信号表。">?</span>'
            "</span>",
            unsafe_allow_html=True,
        )
        rsi_range = st.segmented_control(
            "看板范围",
            options=store.RSI_RANGE_OPTIONS,
            default="近3年",
            selection_mode="single",
            label_visibility="collapsed",
            key=f"rsi_range_{code}",
        )

    data = store.get_rsi_dashboard(code, rsi_range)
    if not data or data.get("nav", pd.DataFrame()).empty:
        st.info("暂无足够净值数据计算 RSI。")
        return

    latest = data.get("latest", {})
    stats = data.get("stats", {})
    signals = data.get("signals", pd.DataFrame())

    # ---- 当日指标卡 ----
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        z = _rsi_zone(latest.get("rsi6"))
        st.markdown(_rsi_kpi_html("日 RSI(6)", f'{latest["rsi6"]:.1f}' if latest.get("rsi6") is not None else "—", z[0], z[1]), unsafe_allow_html=True)
    with c2:
        z = _rsi_zone(latest.get("rsi12"))
        st.markdown(_rsi_kpi_html("日 RSI(12)", f'{latest["rsi12"]:.1f}' if latest.get("rsi12") is not None else "—", z[0], z[1]), unsafe_allow_html=True)
    with c3:
        z = _rsi_zone(latest.get("w_rsi12"))
        st.markdown(_rsi_kpi_html("周 RSI(12)", f'{latest["w_rsi12"]:.1f}' if latest.get("w_rsi12") is not None else "—", z[0], z[1]), unsafe_allow_html=True)
    with c4:
        z = _rsi_zone(latest.get("w_rsi24"))
        st.markdown(_rsi_kpi_html("周 RSI(24)", f'{latest["w_rsi24"]:.1f}' if latest.get("w_rsi24") is not None else "—", z[0], z[1]), unsafe_allow_html=True)
    with c5:
        spread = latest.get("spread")
        spread_text = f"{spread:.2f}%" if spread is not None else "暂无"
        st.markdown(_rsi_kpi_html("股息率利差", spread_text, "", "#8A8F99"), unsafe_allow_html=True)

    # ---- 当前组合判断 + 统计 ----
    signal_text = latest.get("signal_text")
    if signal_text:
        st.markdown(
            f'<div style="background:#FFFFFF;border:1px solid #EDEFF2;border-radius:10px;'
            f'padding:9px 14px;font-size:13px;color:#1F2329;margin:8px 0;">'
            f'<b>📌 当前判断</b>　{signal_text}</div>',
            unsafe_allow_html=True,
        )
    if stats:
        parts = []
        if "buy_count" in stats:
            parts.append(f"买入信号 {stats['buy_count']} 次")
        if "win_rate_60" in stats:
            parts.append(f"T+60 胜率 {stats['win_rate_60']:.0f}%")
        if "avg_fwd_60" in stats:
            parts.append(f"平均 T+60 {stats['avg_fwd_60']:+.1f}%")
        if "avg_mdd60" in stats:
            parts.append(f"平均 60日回撤 {stats['avg_mdd60']:.1f}%")
        if parts:
            st.caption(" · ".join(parts))

    # ---- 三层联动主图 ----
    st.plotly_chart(build_rsi_dashboard_chart(data), width="stretch", config=PLOTLY_CONFIG, key=f"rsi_chart_{code}")

    # ---- 信号明细表 ----
    if not signals.empty:
        _render_rsi_signals_table(signals)


def _render_dividends(code: str) -> None:
    """分红记录：折线图展示累计每份分红。"""
    dividend_df = store.get_dividends(code)
    if dividend_df.empty:
        st.caption("暂无分红记录。")
        return
    st.plotly_chart(build_dividend_history_chart(dividend_df), width="stretch", config=PLOTLY_CONFIG, key="dividend_chart")


def _bond_value_html(value: str | None, unit: str, cls: str) -> str:
    """宫格数值：大数字 + 小单位，语义化配色（红涨/绿风险/中性）。"""
    text = "—" if value is None else value
    unit_html = f'<span class="fc-bond-unit">{unit}</span>' if unit else ""
    return f'<span class="fc-bond-value {cls}">{text}{unit_html}</span>'


def _bond_cell(label: str, value_html: str, *, help_tip: str = "", full: bool = False) -> str:
    """宫格单元格：标签（可带 ⓘ 帮助）+ 数值。full=True 时通栏。"""
    help_html = f'<span class="fc-help" data-tip="{help_tip}">ⓘ</span>' if help_tip else ""
    cls = "fc-bond-item full" if full else "fc-bond-item"
    return (
        f'<div class="{cls}">'
        f'<div class="fc-bond-label">{label}{help_html}</div>'
        f"{value_html}</div>"
    )


def _render_bond_metrics(code: str) -> None:
    """固收+ 核心指标：单卡片 2 列宫格（年化/回撤 | 卡玛/规模 | 年限通栏），语义化着色。"""
    m = store.get_fund_bond_metrics(code)

    def _pct(value) -> str | None:
        if value is None:
            return None
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.2f}"

    def _num(value) -> str | None:
        return None if value is None else f"{value:.2f}"

    ann = _pct(m["annualized_return"])
    mdd = _pct(m["max_drawdown"])
    calmar = _num(m["calmar_ratio"])
    scale = _num(m["fund_scale"])
    age = _num(m["fund_age_years"])

    ann_val = m["annualized_return"]
    ann_cls = "up" if (ann_val is not None and ann_val >= 0) else ("down" if ann_val is not None else "neutral")
    mdd_cls = "down" if m["max_drawdown"] is not None else "neutral"

    card_html = (
        '<div class="fc-bond-card">'
        '<div class="fc-bond-title">固收+ 核心指标</div>'
        '<div class="fc-bond-grid">'
        + _bond_cell("历史年化收益", _bond_value_html(ann, "%", ann_cls))
        + _bond_cell("最大回撤", _bond_value_html(mdd, "%", mdd_cls))
        + _bond_cell(
            "卡玛比率",
            _bond_value_html(calmar, "", "neutral"),
            help_tip="卡玛比率 = 历史年化收益 ÷ |最大回撤|，衡量每承受 1% 回撤能换来多少年化收益",
        )
        + _bond_cell("基金规模", _bond_value_html(scale, " 亿元", "neutral"))
        + _bond_cell("基金年限", _bond_value_html(age, " 年", "neutral"), full=True)
        + "</div></div>"
    )
    st.markdown(card_html, unsafe_allow_html=True)


def render() -> None:
    if not store.is_connected():
        st.warning("请先在侧边栏「数据连接」输入解密口令并连接 Supabase。")
        return

    code = str(st.session_state.get("selected_fund", ""))
    if not code or code not in store.get_fund_codes():
        st.warning("未选择基金，请返回总览页。")
        return

    meta = store.get_fund_meta(code)

    _render_back_and_header(code)
    st.markdown("<br>", unsafe_allow_html=True)

    # ---------- 基金评估（渐进式三层就绪度） ----------
    _render_evaluation(code)

    # ---------- 固收+ 核心指标（历史年化/最大回撤/卡玛/年限/规模） ----------
    if meta["panel"] == "固收":
        _render_bond_metrics(code)

    # ---------- 时间范围胶囊（联动下方所有图表与指标） ----------
    range_key = st.segmented_control(
        "时间范围",
        options=store.RANGE_OPTIONS,
        default="近1年",
        selection_mode="single",
        label_visibility="collapsed",
    )

    nav_df = store.get_nav_history(code, range_key=range_key)

    # ---------- 业绩走势（白底卡片：对比下拉框 + 图例统计条 + 折线图） ----------
    with st.container(border=True, key="perf_panel"):
        _render_performance_chart(code, nav_df, range_key)

    # ---------- 历史净值明细表（白底卡片） ----------
    with st.container(border=True, key="nav_panel"):
        _render_nav_table(code, range_key)

    # ---------- 净值走势（默认折叠） ----------
    with st.expander("📉 净值走势", expanded=False):
        st.plotly_chart(build_nav_area_chart(nav_df), width="stretch", config=PLOTLY_CONFIG, key="nav_area")

    # ---------- 最大回撤（默认折叠） ----------
    with st.expander("📉 最大回撤", expanded=False):
        st.plotly_chart(build_drawdown_area_chart(nav_df), width="stretch", config=PLOTLY_CONFIG, key="drawdown_area")

    # ---------- RSI 动能看板 + 策略信号（红利低波） ----------
    if meta["panel"] == "红利低波":
        _render_rsi_dashboard(code)
        _render_strategy_signal(code)

    # ---------- 分红记录（默认折叠，折线图） ----------
    with st.expander("💰 分红记录", expanded=False):
        _render_dividends(code)
