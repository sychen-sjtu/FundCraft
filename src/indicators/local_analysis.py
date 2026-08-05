"""正式本地分析任务：读取本地快照，计算指标并生成报告。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.indicators.fund_metrics import build_drawdown_series, compute_fund_metrics
from src.storage.local_store import find_latest_raw_snapshot, load_fund_snapshots


def build_summary_report(dataframes: list[pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for df in dataframes:
        metrics = compute_fund_metrics(df)
        rows.append(
            {
                "fund_code": metrics.fund_code,
                "start_date": metrics.start_date,
                "end_date": metrics.end_date,
                "row_count": metrics.row_count,
                "start_unit_nav": metrics.start_unit_nav,
                "end_unit_nav": metrics.end_unit_nav,
                "cumulative_return_pct": round(metrics.cumulative_return_pct, 4),
                "max_drawdown_pct": round(metrics.max_drawdown_pct, 4),
                "annualized_volatility_pct": round(metrics.annualized_volatility_pct, 4),
            }
        )

    return pd.DataFrame(rows).sort_values("fund_code").reset_index(drop=True)


def build_plot(dataframes: list[pd.DataFrame], summary_df: pd.DataFrame) -> go.Figure:
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=False,
        vertical_spacing=0.12,
        row_heights=[0.68, 0.32],
        subplot_titles=("单位净值走势", "回撤走势（%）"),
    )

    colors = ["#2563EB", "#16A34A", "#F97316"]
    for index, df in enumerate(dataframes):
        code = str(df["fund_code"].iloc[0])
        color = colors[index % len(colors)]
        drawdown_df = build_drawdown_series(df)

        figure.add_trace(
            go.Scatter(
                x=df["nav_date"],
                y=df["unit_nav"],
                mode="lines",
                name=f"{code} NAV",
                line=dict(color=color, width=2),
            ),
            row=1,
            col=1,
        )
        figure.add_trace(
            go.Scatter(
                x=drawdown_df["nav_date"],
                y=drawdown_df["drawdown_pct"],
                mode="lines",
                name=f"{code} Drawdown",
                line=dict(color=color, width=2, dash="dot"),
                showlegend=False,
            ),
            row=2,
            col=1,
        )

    figure.update_layout(
        title="FundCraft 阶段 3 本地指标分析",
        template="plotly_white",
        height=900,
        legend_title_text="基金代码 / 指标",
        margin=dict(l=40, r=30, t=80, b=40),
    )
    figure.update_xaxes(title_text="日期", row=2, col=1)
    figure.update_yaxes(title_text="单位净值", row=1, col=1)
    figure.update_yaxes(title_text="回撤 %", row=2, col=1)
    return figure


def run_local_analysis(project_root: Path | None = None) -> tuple[Path, Path]:
    """执行一次本地分析，返回 (汇总 CSV 路径, HTML 报告路径)。"""
    root = project_root or Path(__file__).resolve().parents[2]
    snapshot_dir = find_latest_raw_snapshot(root)
    dataframes = load_fund_snapshots(snapshot_dir)
    summary_df = build_summary_report(dataframes)

    processed_dir = root / "data" / "processed" / "stage3_local"
    processed_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = processed_dir / f"summary_{timestamp}.csv"
    html_path = processed_dir / f"report_{timestamp}.html"

    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    figure = build_plot(dataframes, summary_df)
    figure.write_html(html_path, include_plotlyjs="cdn")

    print(f"Snapshot directory: {snapshot_dir}")
    print("Summary metrics:")
    print(summary_df.to_string(index=False))
    print(f"Saved summary CSV to: {summary_path}")
    print(f"Saved HTML report to: {html_path}")
    return summary_path, html_path


if __name__ == "__main__":
    run_local_analysis()
