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
    """
    items_html = []
    for item in indexes:
        value = item.get("value")
        change = item.get("change_pct")
        if value is None or change is None:
            continue
        cls = change_class(change)
        sign = "+" if change > 0 else ""
        items_html.append(
            f'<div class="fc-index-item">'
            f'<div class="fc-index-name">{item.get("name", "")}</div>'
            f'<div class="fc-index-value fc-num">{value:,.2f}</div>'
            f'<div class="{cls} fc-num">{sign}{change:.2f}%</div>'
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
) -> str:
    """渲染一只基金卡片的主要信息 HTML（不含按钮，按钮由调用方添加）。

    :param period_returns: {"近1周": 1.23, "近1月": -0.45, "近3月": 2.10}
    """
    period_returns = period_returns or {}
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
    return (
        f'<div class="fc-fund-card">'
        f'<div><span class="fc-fund-name">{fund_name}</span>{tag_html}</div>'
        f'<div class="fc-fund-code">基金代码：{fund_code}</div>'
        f'<div style="margin-top:10px; display:flex; justify-content:space-between; align-items:flex-end;">'
        f"<div>{nav_block}</div>"
        f"</div>"
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
