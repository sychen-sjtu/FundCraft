"""FundCraft 全局 UI 主题。

对齐支付宝理财 / 天天基金等主流基金 App 的风格：
- 中国市场配色约定：红涨绿跌（正收益红色、负收益绿色）。
- 卡片式布局、大号净值数字、统一浅灰页面底色。
- 提供 CSS 注入、涨跌颜色渲染、基金卡片 HTML 等通用组件。

说明：本文件只负责"长相"，不关心数据来源。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

# ---------- 配色常量 ----------
COLOR_UP = "#E64A3D"      # 上涨 / 正收益（红）
COLOR_DOWN = "#00B578"    # 下跌 / 负收益（绿）
COLOR_PRIMARY = "#1677FF"  # 主题蓝（支付宝主色）

# 图表焦点与对比配色：主基金高亮暖色 / 对比指数极淡灰（支付宝风格）
COLOR_FUND_HIGHLIGHT = "#FF6B00"  # 主基金（对比视图）高亮橙
COLOR_BENCHMARK = "#D9D9D9"       # 对比指数极淡灰（背景化，不抢主线）

# 图表序列配色（多序列对比时循环使用）
CHART_COLORS = ["#1677FF", "#E64A3D", "#00B578", "#FA8C16", "#722ED1", "#13C2C2"]

# Plotly 图表交互配置：不显示工具条、禁用滚轮/双击缩放；
# 时间范围由页面胶囊控制，拖拽/缩放会导致误操作，这里统一禁用（保留 hover 显示数值）。
PLOTLY_CONFIG = {
    "displayModeBar": False,
    "scrollZoom": False,
    "doubleClick": False,
}

GLOBAL_CSS = """
<style>
/* 页面底色 */
[data-testid="stAppViewContainer"] { background-color: #F5F6F8; }
[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 4rem; padding-bottom: 3rem; }

/* 让数字等宽对齐，避免跳动 */
.fc-num { font-variant-numeric: tabular-nums; }

/* 涨跌颜色 */
.fc-up   { color: #E64A3D; font-weight: 600; }
.fc-down { color: #00B578; font-weight: 600; }
.fc-flat { color: #8A8F99; font-weight: 600; }

/* 概览指标卡 */
.fc-metric-card {
  background: #FFFFFF;
  border: 1px solid #EDEFF2;
  border-radius: 12px;
  padding: 14px 16px;
  height: 100%;
  box-shadow: 0 1px 2px rgba(31,35,41,0.04);
}
.fc-metric-label { font-size: 13px; color: #8A8F99; margin-bottom: 4px; }
.fc-metric-value { font-size: 22px; font-weight: 700; color: #1F2329; }
.fc-metric-delta { font-size: 13px; margin-top: 2px; }

/* 市场指数条 */
.fc-index-bar {
  background: #FFFFFF;
  border: 1px solid #EDEFF2;
  border-radius: 12px;
  padding: 10px 18px;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  box-shadow: 0 1px 2px rgba(31,35,41,0.04);
}
.fc-index-item { text-align: center; flex: 1; }
.fc-index-name { font-size: 13px; color: #8A8F99; }
.fc-index-value { font-size: 18px; font-weight: 700; margin-top: 2px; }
.fc-index-change { font-size: 12px; margin-top: 2px; }

/* 基金卡片 */
.fc-fund-card {
  background: #FFFFFF;
  border: 1px solid #EDEFF2;
  border-radius: 12px;
  padding: 14px 16px;
  box-shadow: 0 1px 2px rgba(31,35,41,0.04);
}
.fc-fund-name { font-size: 16px; font-weight: 700; color: #1F2329; }
.fc-tag {
  display: inline-block;
  font-size: 12px;
  color: #1677FF;
  background: #EAF2FF;
  border-radius: 4px;
  padding: 1px 6px;
  margin-left: 6px;
}
.fc-tag-gray {
  display: inline-block;
  font-size: 12px;
  color: #8A8F99;
  background: #F2F3F5;
  border-radius: 4px;
  padding: 1px 6px;
  margin-left: 6px;
}
.fc-fund-code { font-size: 12px; color: #8A8F99; margin-top: 2px; }
.fc-nav-big { font-size: 30px; font-weight: 800; color: #1F2329; line-height: 1.1; }
.fc-nav-change { font-size: 14px; font-weight: 600; margin-top: 2px; }
.fc-period-row { display: flex; gap: 22px; margin-top: 8px; }
.fc-period-item { text-align: left; }
.fc-period-label { font-size: 12px; color: #8A8F99; }
.fc-period-value { font-size: 14px; font-weight: 600; margin-top: 2px; }

/* 详情页大净值头部 */
.fc-detail-head {
  background: linear-gradient(135deg, #FFFFFF 0%, #F7FAFF 100%);
  border: 1px solid #EDEFF2;
  border-radius: 14px;
  padding: 20px 24px;
  box-shadow: 0 1px 3px rgba(31,35,41,0.05);
}
.fc-detail-nav { font-size: 42px; font-weight: 800; line-height: 1; }
.fc-detail-change { font-size: 16px; font-weight: 700; margin-top: 6px; }

/* 策略信号卡 */
.fc-signal-buy {
  display: inline-block;
  background: #E64A3D;
  color: #FFFFFF;
  border-radius: 6px;
  padding: 2px 12px;
  font-size: 14px;
  font-weight: 700;
}
.fc-signal-wait {
  display: inline-block;
  background: #F2F3F5;
  color: #8A8F99;
  border-radius: 6px;
  padding: 2px 12px;
  font-size: 14px;
  font-weight: 700;
}

/* 当日指标高亮标签 */
.fc-today-tag {
  display: inline-block;
  font-size: 12px;
  color: #FFFFFF;
  background: #1677FF;
  border-radius: 4px;
  padding: 1px 8px;
  margin-right: 6px;
  vertical-align: middle;
}

/* 指标卡：当日突出显示（蓝色左边框 + 浅蓝底） */
.fc-metric-today {
  background: #FFFFFF;
  border: 1px solid #D6E4FF;
  border-left: 3px solid #1677FF;
  border-radius: 12px;
  padding: 14px 16px;
  height: 100%;
  box-shadow: 0 1px 2px rgba(31,35,41,0.04);
}

/* 侧边栏微调 */
[data-testid="stSidebar"] { background: #FFFFFF; }

/* 开屏解锁：统一表单卡片（Logo/标题/口令/按钮全部在卡片内） */
.lock-spacer { height: 3vh; }
.st-key-lock_panel {
  background: #FFFFFF !important;
  border: 1px solid #EDEFF2 !important;
  border-radius: 14px !important;
  padding: 32px 30px 28px !important;
  box-shadow: 0 6px 24px rgba(31,35,41,0.08) !important;
}
.lock-head { text-align: center; }
.lock-icon { font-size: 48px; line-height: 1; }
.lock-app { font-size: 24px; font-weight: 800; color: #1F2329; margin-top: 10px; }
.lock-sub { font-size: 13px; color: #8A8F99; margin-top: 6px; }
.lock-divider { height: 1px; background: #EDEFF2; margin: 22px 0 18px; }
.lock-title { text-align: center; font-size: 14px; font-weight: 600; color: #1F2329; margin-bottom: 8px; }
.lock-gap { height: 14px; }

/* 主按钮：项目蓝色主题（覆盖 Streamlit 默认红色，避免刺眼） */
[data-testid="stBaseButton-primary"] {
  background-color: #1677FF !important;
  border-color: #1677FF !important;
  color: #FFFFFF !important;
}
[data-testid="stBaseButton-primary"]:hover {
  background-color: #0F5FD6 !important;
  border-color: #0F5FD6 !important;
}
[data-testid="stBaseButton-primary"]:active {
  background-color: #0A4FC4 !important;
  border-color: #0A4FC4 !important;
}

/* 输入框更醒目（清晰描边 + 聚焦蓝色高亮） */
[data-testid="stTextInput"] input,
[data-testid="stTextInputRootElement"] input {
  border: 1.5px solid #B9C0CC !important;
  border-radius: 8px !important;
  background-color: #FFFFFF !important;
}
[data-testid="stTextInput"] input:focus,
[data-testid="stTextInputRootElement"] input:focus {
  border-color: #1677FF !important;
  box-shadow: 0 0 0 3px rgba(22,119,255,0.12) !important;
}

/* 白底圆角卡片（业绩走势 / 历史净值等区域） */
.st-key-perf_panel, .st-key-nav_panel {
  background: #FFFFFF;
  border: 1px solid #EDEFF2 !important;
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(31,35,41,0.04);
}

/* 卡片标题行：标题 + 右上角浅灰说明 */
.fc-panel-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.fc-panel-title { font-size: 15px; font-weight: 700; color: #1F2329; }
.fc-panel-note { font-size: 12px; color: #B0B6BF; }



/* 图例 + 最新涨跌幅统计条（折线图正上方） */
.fc-legend-strip { display: flex; gap: 28px; flex-wrap: wrap; margin: 2px 0 8px; }
.fc-legend-item { display: flex; align-items: center; gap: 6px; font-size: 13px; color: #595959; }
.fc-legend-swatch { display: inline-block; border-radius: 2px; background: #D9D9D9; }
.fc-legend-value { font-weight: 700; font-variant-numeric: tabular-nums; }
.fc-legend-value.up { color: #E64A3D; }   /* 涨→红（语义化） */
.fc-legend-value.down { color: #00B578; } /* 跌→绿（语义化） */
.fc-legend-value.flat { color: #8A8F99; }

/* 历史净值表：无外边框 + 细行分割线 + 对齐（支付宝轻量表格） */
.fc-nav-table-wrap { max-height: 320px; overflow-y: auto; border-radius: 8px; }
.fc-nav-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.fc-nav-table thead th {
  text-align: left; color: #8C8C8C; font-weight: 500;
  border-bottom: 1px solid #F0F0F0; padding: 8px 6px; font-size: 12px;
  position: sticky; top: 0; background: #FFFFFF;
}
.fc-nav-table tbody td {
  text-align: left; color: #262626; padding: 7px 6px;
  border-bottom: 1px solid #F0F0F0; font-variant-numeric: tabular-nums;
}
.fc-nav-table .num { text-align: right; }
.fc-nav-table tbody tr:last-child td { border-bottom: none; }

/* 扁平化下拉框（对比指数）：浅灰底、无重框 */
[data-testid="stSelectbox"] [data-baseweb="select"] > div:first-child {
  background: #F5F6F8 !important;
  border-color: transparent !important;
  border-radius: 8px !important;
}
[data-testid="stSelectbox"]:focus-within [data-baseweb="select"] > div:first-child {
  box-shadow: 0 0 0 2px rgba(22,119,255,0.15) !important;
}

/* 债券基金核心指标宫格卡片（固收+/债基 共用，单卡片 2 列 + 底行通栏） */
.fc-bond-card {
  background: #FFFFFF;
  border: 1px solid #EDEFF2;
  border-radius: 12px;
  padding: 14px 18px 12px;
  box-shadow: 0 1px 3px rgba(31,35,41,0.04);
}
.fc-bond-title { font-size: 15px; font-weight: 700; color: #1F2329; }
.fc-bond-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0; margin-top: 10px; }
.fc-bond-item { padding: 12px 6px; border-top: 1px solid #F0F0F0; }
.fc-bond-item:nth-child(1),
.fc-bond-item:nth-child(2) { border-top: none; }
.fc-bond-item:nth-child(odd) { border-right: 1px solid #F0F0F0; }
.fc-bond-item.full { grid-column: 1 / -1; border-right: none; }
.fc-bond-label {
  font-size: 12px; color: #8C8C8C;
  display: flex; align-items: center; gap: 4px;
}
.fc-bond-value {
  font-size: 20px; font-weight: 800; color: #1F1F1F;
  font-variant-numeric: tabular-nums; margin-top: 2px;
}
.fc-bond-value.up { color: #E64A3D; }   /* 年化收益（红涨） */
.fc-bond-value.down { color: #00B578; } /* 最大回撤（风险绿） */
.fc-bond-unit { font-size: 12px; font-weight: 500; color: #8A8F99; margin-left: 1px; }

/* “ⓘ / ?”帮助（纯 CSS hover 气泡，iframe 内可用；卡玛比率与业绩走势共用） */
.fc-help {
  display: inline-block; width: 13px; height: 13px; line-height: 13px;
  text-align: center; border-radius: 50%; background: #E9EBEF; color: #8A8F99;
  font-size: 10px; font-weight: 700; cursor: help;
  position: relative; vertical-align: 1px; flex: none;
}
.fc-help::after {
  content: attr(data-tip);
  position: absolute; left: 50%; bottom: calc(100% + 6px); transform: translateX(-50%);
  width: 240px; max-width: 70vw; padding: 7px 9px; border-radius: 6px;
  background: rgba(31,35,41,0.92); color: #FFFFFF; font-size: 11px; line-height: 1.5;
  font-weight: 400; text-align: left; white-space: normal;
  opacity: 0; visibility: hidden; transition: opacity 0.15s ease;
  pointer-events: none; z-index: 60;
}
.fc-help:hover::after { opacity: 1; visibility: visible; }
</style>
"""


def inject_global_css() -> None:
    """向页面注入全局样式（在每页渲染前调用一次）。"""
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)


# ---------- 涨跌颜色渲染 ----------
def change_class(value) -> str:
    """根据数值返回涨跌 CSS 类名。"""
    if value is None:
        return "fc-flat"
    try:
        if float(value) > 0:
            return "fc-up"
        if float(value) < 0:
            return "fc-down"
    except (TypeError, ValueError):
        return "fc-flat"
    return "fc-flat"


def change_html(value, suffix: str = "%", digits: int = 2, plus: bool = True) -> str:
    """渲染带涨跌颜色的数值 HTML（如 `+1.23%` / `-0.45%`）。"""
    if value is None:
        return '<span class="fc-flat fc-num">—</span>'
    try:
        value = float(value)
    except (TypeError, ValueError):
        return '<span class="fc-flat fc-num">—</span>'
    cls = change_class(value)
    sign = "+" if (plus and value > 0) else ""
    return f'<span class="{cls} fc-num">{sign}{value:.{digits}f}{suffix}</span>'


# ---------- 概览指标卡 ----------
def render_metric_card(label: str, value: str, delta_html: str = "") -> None:
    """渲染一张概览指标卡（白底圆角卡片）。"""
    delta_block = f'<div class="fc-metric-delta">{delta_html}</div>' if delta_html else ""
    st.markdown(
        f'<div class="fc-metric-card"><div class="fc-metric-label">{label}</div>'
        f'<div class="fc-metric-value">{value}</div>{delta_block}</div>',
        unsafe_allow_html=True,
    )


# ---------- 市场指数条 ----------
def render_index_bar(indexes: list[dict]) -> None:
    """渲染顶部市场指数条（支付宝首页风格）。

    :param indexes: [{"name": "上证指数", "value": 3405.12, "change_pct": 0.82}, ...]
    配置了但无行情数据的指数也会展示（显示「暂无」），保证配置项都可见。
    """
    items_html = []
    for item in indexes:
        value = item.get("value")
        change = item.get("change_pct")
        name = item.get("name", "")
        if value is None:
            items_html.append(
                f'<div class="fc-index-item">'
                f'<div class="fc-index-name">{name}</div>'
                f'<div class="fc-index-value fc-num fc-flat">—</div>'
                f'<div class="fc-index-change fc-flat">暂无</div>'
                f"</div>"
            )
            continue
        cls = change_class(change)
        sign = "+" if change and change > 0 else ""
        change_text = "—" if change is None else f"{sign}{change:.2f}%"
        items_html.append(
            f'<div class="fc-index-item">'
            f'<div class="fc-index-name">{name}</div>'
            f'<div class="fc-index-value fc-num">{value:,.2f}</div>'
            f'<div class="{cls} fc-num">{change_text}</div>'
            f"</div>"
        )
    if not items_html:
        return
    st.markdown(f'<div class="fc-index-bar">{"".join(items_html)}</div>', unsafe_allow_html=True)


# ---------- 基金卡片 ----------
def fund_card_html(
    fund_name: str,
    fund_code: str,
    category: str,
    latest_nav: float | None,
    daily_change: float | None,
    period_returns: dict[str, float | None] | None = None,
    show_nav: bool = True,
) -> str:
    """渲染一只基金卡片的主要信息 HTML（不含按钮，按钮由调用方添加）。

    :param period_returns: {"近1周": 1.23, "近1月": -0.45, "近3月": 2.10}
    :param show_nav: False 时不渲染最新净值与日涨跌（总览页只保留简略信息）。
    """
    period_returns = period_returns or {}

    nav_block = ""
    if show_nav:
        nav_text = "—" if latest_nav is None else f"{latest_nav:.4f}"
        nav_block = (
            f'<div class="fc-nav-big fc-num">{nav_text}</div>'
            f'<div class="fc-nav-change">{change_html(daily_change, suffix="%", digits=2)}</div>'
            if latest_nav is not None
            else '<div class="fc-nav-big fc-flat">—</div>'
        )

    period_items = []
    for label in ("近1周", "近1月", "近3月"):
        val = period_returns.get(label)
        period_items.append(
            f'<div class="fc-period-item"><div class="fc-period-label">{label}</div>'
            f'<div class="fc-period-value">{change_html(val)}</div></div>'
        )
    period_html = f'<div class="fc-period-row">{"".join(period_items)}</div>' if period_items else ""

    tag_html = f'<span class="fc-tag">{category}</span>' if category else ""
    nav_row = (
        f'<div style="margin-top:10px; display:flex; justify-content:space-between; align-items:flex-end;">'
        f"<div>{nav_block}</div>"
        f"</div>"
        if nav_block
        else ""
    )
    return (
        f'<div class="fc-fund-card">'
        f'<div><span class="fc-fund-name">{fund_name}</span>{tag_html}</div>'
        f'<div class="fc-fund-code">基金代码：{fund_code}</div>'
        f"{nav_row}"
        f"{period_html}"
        f"</div>"
    )


# ---------- 详情页头部 ----------
def detail_head_html(
    fund_name: str,
    fund_code: str,
    category: str,
    latest_nav: float | None,
    daily_change: float | None,
    nav_date: str = "",
) -> str:
    tag_html = f'<span class="fc-tag">{category}</span>' if category else ""
    nav_text = "—" if latest_nav is None else f"{latest_nav:.4f}"
    date_html = f'<span style="font-size:13px;color:#8A8F99;margin-left:8px;">{nav_date}</span>' if nav_date else ""
    return (
        f'<div class="fc-detail-head">'
        f'<div><span class="fc-fund-name" style="font-size:20px;">{fund_name}</span>{tag_html}'
        f'<span class="fc-tag-gray">{fund_code}</span></div>'
        f'<div style="margin-top:14px; display:flex; align-items:baseline;">'
        f'<div class="fc-detail-nav fc-num">{nav_text}</div>'
        f'<div class="fc-detail-change" style="margin-left:16px;">{change_html(daily_change)}</div>'
        f"</div>{date_html}</div>"
    )
