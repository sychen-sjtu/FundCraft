from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.fetchers.akshare_fund_nav import normalize_fund_code
from src.indicators.fund_metrics import build_drawdown_series, compute_fund_metrics


def find_latest_raw_snapshot(root: Path) -> Path:
    raw_root = root / "data" / "raw" / "fund_nav"
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw fund NAV directory not found: {raw_root}")

    snapshot_dirs = [path for path in raw_root.iterdir() if path.is_dir()]
    if not snapshot_dirs:
        raise FileNotFoundError(f"No snapshot directories found under: {raw_root}")

    return sorted(snapshot_dirs)[-1]


def load_fund_data(snapshot_dir: Path) -> list[pd.DataFrame]:
    dataframes: list[pd.DataFrame] = []
    for csv_path in sorted(snapshot_dir.glob("*.csv")):
        if csv_path.name == "manifest.csv":
            continue
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        if "fund_code" in df.columns:
            df["fund_code"] = df["fund_code"].astype(str).str.strip().apply(normalize_fund_code)
        df["nav_date"] = pd.to_datetime(df["nav_date"], errors="coerce")
        df["unit_nav"] = pd.to_numeric(df["unit_nav"], errors="coerce")
        df = df.dropna(subset=["nav_date", "unit_nav"])
        dataframes.append(df)

    if not dataframes:
        raise FileNotFoundError(f"No fund CSV files found in: {snapshot_dir}")

    return dataframes


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


def main() -> None:
    snapshot_dir = find_latest_raw_snapshot(PROJECT_ROOT)
    dataframes = load_fund_data(snapshot_dir)
    summary_df = build_summary_report(dataframes)

    processed_dir = PROJECT_ROOT / "data" / "processed" / "stage3_local"
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


if __name__ == "__main__":
    main()