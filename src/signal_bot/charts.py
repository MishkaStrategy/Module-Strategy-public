from __future__ import annotations

import tempfile
from pathlib import Path

from .domain import CandleBatch, TradeSignal


class MatplotlibChartRenderer:
    def __init__(self, *, theme: str = "dark", width: int = 1600, height: int = 900) -> None:
        if theme not in {"dark", "light"}:
            raise ValueError("chart theme must be dark or light")
        self.theme, self.width, self.height = theme, width, height

    def render(self, signal: TradeSignal, batch: CandleBatch) -> Path:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from matplotlib.patches import Rectangle
        except ImportError as exc:
            raise RuntimeError("install the signal-bot extra to render charts") from exc
        dark = self.theme == "dark"
        bg, fg, grid = (
            ("#10151d", "#e7edf4", "#263242") if dark else ("#ffffff", "#18212b", "#dbe2ea")
        )
        candles = batch.candles[-120:]
        dpi = 160
        fig, ax = plt.subplots(figsize=(self.width / dpi, self.height / dpi), dpi=dpi)
        fig.patch.set_facecolor(bg)
        ax.set_facecolor(bg)
        for x, bar in enumerate(candles):
            up = bar.close >= bar.open
            color = "#21c77a" if up else "#ff5b68"
            ax.vlines(x, bar.low, bar.high, color=color, linewidth=1.1)
            bottom, height = min(bar.open, bar.close), abs(bar.close - bar.open)
            height = max(height, float(signal.entry_price) * 1e-7)
            ax.add_patch(
                Rectangle((x - 0.32, bottom), 0.64, height, facecolor=color, edgecolor=color)
            )
        levels = [
            (signal.entry_price, "ENTRY", "#52a8ff"),
            (signal.stop_loss, "STOP", "#ff5b68"),
        ]
        levels += [
            (value, f"TP{i}", "#21c77a") for i, value in enumerate(signal.take_profit_levels, 1)
        ]
        x0, x1 = -0.5, len(candles) - 0.5
        risk_color, reward_color = "#ff5b68", "#21c77a"
        ax.axhspan(
            float(min(signal.entry_price, signal.stop_loss)),
            float(max(signal.entry_price, signal.stop_loss)),
            color=risk_color,
            alpha=0.10,
        )
        if signal.take_profit_levels:
            far = signal.take_profit_levels[-1]
            ax.axhspan(
                float(min(signal.entry_price, far)),
                float(max(signal.entry_price, far)),
                color=reward_color,
                alpha=0.07,
            )
        for value, label, color in levels:
            ax.axhline(float(value), color=color, linewidth=1.15, linestyle="--")
            ax.text(
                x1,
                float(value),
                f" {label}  {value}",
                color=color,
                fontsize=9,
                va="bottom",
                ha="right",
                bbox={"facecolor": bg, "edgecolor": "none", "alpha": 0.85},
            )
        ax.scatter(
            len(candles) - 1,
            float(candles[-1].close),
            marker="^" if signal.side.value == "buy" else "v",
            s=90,
            color="#ffd166",
            zorder=5,
        )
        ratio = signal.risk_reward[-1] if signal.risk_reward else None
        rr = f" • {ratio:.2f}R" if ratio is not None else ""
        ax.set_title(
            f"{signal.symbol} • {signal.signal_timeframe} • {signal.side.value.upper()}{rr}\n"
            f"{signal.strategy_id} v{signal.strategy_version} • {signal.signal_timestamp:%Y-%m-%d %H:%M UTC}",
            color=fg,
            loc="left",
            fontsize=13,
            pad=12,
        )
        ax.grid(color=grid, alpha=0.45, linewidth=0.6)
        ax.tick_params(colors=fg, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(grid)
        ax.set_xlim(x0, x1)
        ax.margins(y=0.12)
        fig.tight_layout()
        handle = tempfile.NamedTemporaryFile(prefix="signal-", suffix=".png", delete=False)
        path = Path(handle.name)
        handle.close()
        fig.savefig(path, facecolor=bg, bbox_inches="tight")
        plt.close(fig)
        return path
