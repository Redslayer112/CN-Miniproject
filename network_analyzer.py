"""
network_analyzer.py
Reads download_log CSV(s) from network_downloader.py and produces:
  - Console summary
  - <prefix>_charts.png        (6-panel combined chart + status pie)
  - Individual PNGs per panel  (01_speed_per_run.png … 07_status_breakdown.png)
  - report.txt                 (full narrative analysis)

Requires: numpy, pandas, matplotlib, scipy
Install:  pip install numpy pandas matplotlib scipy

Usage:
    python network_analyzer.py download_log.csv
    python network_analyzer.py log1.csv log2.csv --output my_report --results-dir out/
"""

import argparse, sys
from datetime import datetime
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    from matplotlib.gridspec import GridSpec
    from matplotlib.patches import Patch
    from scipy import stats as scipy_stats
except ImportError as exc:
    sys.exit(f"Missing dependency: {exc.name}\n"
             "Install with:  pip install numpy pandas matplotlib scipy")

# ── Chart colours ─────────────────────────────────────────────────────────────
ACCENT    = "#00d4ff"
WARN      = "#ff5555"
SUCCESS_C = "#00ff99"
PARTIAL_C = "#ffaa00"
SKIP_C    = "#ff5555"
GRID_C    = "#2a2a3a"
TEXT_C    = "#e0e0f0"
BG_FIG    = "#0f0f1a"
BG_AX     = "#181828"
LAT_C     = "#cc88ff"
LOSS_C    = "#ff8844"


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _parse_ts(s: str) -> datetime:
    """Parse HH:MM:SS:mmm string into a datetime."""
    hh, mm, ss, ms = (int(x) for x in str(s).strip().split(":"))
    return datetime(1900, 1, 1, hh, mm, ss, ms * 1000)


def load_csv(path: str) -> pd.DataFrame:
    """Load one CSV, coerce numeric columns, and derive hour + missing duration."""
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    for col in ["run_number", "duration_s", "speed_Mbps", "packet_loss_pct", "latency_ms"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["status"] = df["status"].fillna("SKIPPED").str.upper()

    # Parse start_time → datetime so we can extract hour of day
    mask = df["start_time"].notna()
    df.loc[mask, "start_dt"] = df.loc[mask, "start_time"].apply(_parse_ts)
    df["hour"] = df["start_dt"].dt.hour

    # Derive duration_s from timestamps if the column is missing / NaN
    if "duration_s" not in df.columns:
        df["duration_s"] = np.nan
    missing = df["duration_s"].isna() & df["start_time"].notna() & df["end_time"].notna()
    if missing.any():
        end_dt = df.loc[missing, "end_time"].apply(_parse_ts)
        dur = (end_dt - df.loc[missing, "start_dt"]).dt.total_seconds()
        dur[dur < 0] += 86400          # handle midnight roll-over
        df.loc[missing, "duration_s"] = dur.values

    return df.reset_index(drop=True)


def load_all_csvs(paths: list) -> pd.DataFrame:
    """Load and concatenate multiple CSVs; sort by run_number."""
    frames = []
    for p in paths:
        if not Path(p).exists():
            print(f"[WARN] File not found: {p} -- skipping.")
            continue
        frames.append(load_csv(p))
    if not frames:
        sys.exit("No valid CSV files found.")
    df = pd.concat(frames, ignore_index=True)
    if "run_number" in df.columns:
        df.sort_values("run_number", inplace=True)
    return df.reset_index(drop=True)


# ── Statistics ────────────────────────────────────────────────────────────────

def compute_stats(df: pd.DataFrame) -> dict:
    """
    Split runs by status, compute speed / latency / packet-loss stats,
    run linear regression on speed over time, and diagnose congestion vs throttling.
    Returns a flat dict consumed by the chart and report functions.
    """
    success = df[df["status"] == "SUCCESS"].copy()
    partial = df[df["status"] == "PARTIAL"].copy()
    skipped = df[df["status"] == "SKIPPED"].copy()

    speed = success["speed_Mbps"].dropna()
    if len(speed) == 0:
        sys.exit("[ERROR] No SUCCESS rows found — nothing to analyse.")
    if len(speed) < 2:
        print(
            f"[WARN] Only {len(speed)} SUCCESS row found. "
            "Trend analysis requires at least 2 — skipping regression."
        )

    # Hourly aggregates (mean / median / std / min / max per hour of day)
    hourly = success.groupby("hour")["speed_Mbps"].agg(
        mean="mean", median="median", std="std", min="min", max="max", count="count"
    ).reset_index()

    p25 = float(speed.quantile(0.25))
    hourly["congested"] = hourly["mean"] < p25          # hours below P25 = congested
    busiest_hour = int(hourly.loc[hourly["mean"].idxmin(), "hour"])
    fastest_hour = int(hourly.loc[hourly["mean"].idxmax(), "hour"])

    lat = df[df["status"].isin(["SUCCESS", "PARTIAL"])]["latency_ms"].dropna()
    pl  = df["packet_loss_pct"].dropna()

    # Linear regression: is speed trending up or down over runs?
    x = np.arange(len(success))
    slope, _, r_value, p_value, _ = scipy_stats.linregress(x, success["speed_Mbps"].values)

    # Congestion: high packet loss + low speed → congestion
    # Throttling: low packet loss + low speed → ISP cap
    pl_success = success["packet_loss_pct"].dropna()
    if len(pl_success) and len(speed):
        speed_p25      = float(speed.quantile(0.25))
        hi_pl_lo_speed = success[(success["packet_loss_pct"] > 5) & (success["speed_Mbps"] < speed_p25)]
        lo_pl_lo_speed = success[(success["packet_loss_pct"] <= 5) & (success["speed_Mbps"] < speed_p25)]
        congestion_pct = len(hi_pl_lo_speed) / max(len(success), 1) * 100
        throttle_pct   = len(lo_pl_lo_speed) / max(len(success), 1) * 100
    else:
        congestion_pct = throttle_pct = 0.0

    return dict(
        df_success=success, df_partial=partial, df_skipped=skipped,
        hourly=hourly, busiest_hour=busiest_hour, fastest_hour=fastest_hour,
        overall_mean=float(speed.mean()),   overall_median=float(speed.median()),
        overall_std=float(speed.std()),     overall_min=float(speed.min()),
        overall_max=float(speed.max()),
        cv=float(speed.std() / speed.mean() * 100) if speed.mean() > 0 else 0.0,
        slope=slope, r_value=r_value, p_value=p_value, p25=p25,
        total_runs=len(df), n_success=len(success),
        n_partial=len(partial), n_skipped=len(skipped),
        avg_latency=float(lat.mean()) if len(lat) else 0.0,
        max_latency=float(lat.max())  if len(lat) else 0.0,
        min_latency=float(lat.min())  if len(lat) else 0.0,
        avg_packet_loss=float(pl.mean()) if len(pl) else 0.0,
        max_packet_loss=float(pl.max())  if len(pl) else 0.0,
        avg_duration=float(success["duration_s"].dropna().mean()) if len(success) else 0.0,
        congestion_pct=congestion_pct, throttle_pct=throttle_pct,
    )


# ── Chart helpers ─────────────────────────────────────────────────────────────

def _style_ax(ax, title: str) -> None:
    """Apply dark-theme styling to an Axes."""
    ax.set_facecolor(BG_AX)
    ax.set_title(title, color=TEXT_C, fontsize=11, fontweight="bold", pad=10)
    ax.tick_params(colors=TEXT_C, labelsize=8)
    ax.xaxis.label.set_color(TEXT_C)
    ax.yaxis.label.set_color(TEXT_C)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID_C)
    ax.grid(color=GRID_C, linestyle="--", linewidth=0.5, alpha=0.6)


def _add_speed_line(ax, runs, success, stats):
    """Shared helper: plot speed line + trend + mean + skipped/partial markers."""
    ax.plot(runs, success["speed_Mbps"], color=ACCENT, lw=1.6,
            marker="o", markersize=4, label="Speed (Mbps)", zorder=3)
    x    = np.arange(len(success))
    m, b = stats["slope"], stats["overall_mean"] - stats["slope"] * x.mean()
    ax.plot(runs, m * x + b, color=WARN, lw=1.4, linestyle="--", label="Trend")
    ax.axhline(stats["overall_mean"], color="#ffcc00", lw=1.1, linestyle=":",
               label=f"Mean {stats['overall_mean']:.1f} Mbps")
    for pr in stats["df_partial"]["run_number"].dropna().values:
        ax.axvline(pr, color=PARTIAL_C, lw=1.0, linestyle=":", alpha=0.7)
    for sr in stats["df_skipped"]["run_number"].dropna().values:
        ax.axvline(sr, color=SKIP_C,    lw=1.0, linestyle=":", alpha=0.5)
    ax.set_xlabel("Run #"); ax.set_ylabel("Speed (Mbps)")
    extra = [Patch(color=SKIP_C,    label=f"Skipped ({stats['n_skipped']})"),
             Patch(color=PARTIAL_C, label=f"Partial ({stats['n_partial']})")]
    h, l = ax.get_legend_handles_labels()
    ax.legend(h + extra, l + [e.get_label() for e in extra],
              fontsize=7.5, facecolor=BG_AX, edgecolor=GRID_C, labelcolor=TEXT_C)


def _draw_panels(axes, success, runs, stats):
    """
    Draw all 6 data panels onto a list/dict of Axes.
    axes = [ax1, ax2, ax3, ax4, ax5, ax6]
    """
    ax1, ax2, ax3, ax4, ax5, ax6 = axes
    pl_vals  = success["packet_loss_pct"].values
    lat_vals = success["latency_ms"].values
    dur_vals = success["duration_s"].values
    hourly   = stats["hourly"]

    # 1 — Speed per run (line + trend)
    _style_ax(ax1, "Download Speed per Run")
    _add_speed_line(ax1, runs, success, stats)

    # 2 — Speed vs Packet Loss (dual y-axis)
    _style_ax(ax2, "Speed vs Packet Loss per Run")
    ax2.plot(runs, success["speed_Mbps"], color=ACCENT, lw=1.5,
             marker="o", markersize=3.5, label="Speed (Mbps)")
    ax2r = ax2.twinx()
    ax2r.bar(runs, pl_vals, color=LOSS_C, alpha=0.4, width=0.7, label="Packet Loss (%)")
    ax2r.plot(runs, pl_vals, color=LOSS_C, lw=1.1, linestyle="--")
    ax2r.set_ylabel("Packet Loss (%)", color=LOSS_C, fontsize=9)
    ax2r.tick_params(colors=LOSS_C, labelsize=8)
    ax2r.spines["right"].set_edgecolor(LOSS_C)
    ax2r.set_ylim(0, max(float(np.max(pl_vals)) * 1.3, 10))
    ax2.set_xlabel("Run #"); ax2.set_ylabel("Speed (Mbps)")
    l1, lb1 = ax2.get_legend_handles_labels()
    l2, lb2 = ax2r.get_legend_handles_labels()
    ax2.legend(l1 + l2, lb1 + lb2,
               fontsize=7.5, facecolor=BG_AX, edgecolor=GRID_C, labelcolor=TEXT_C)

    # 3 — Hourly average speed (bar; congested hours highlighted in WARN colour)
    _style_ax(ax3, "Average Speed by Hour of Day")
    bar_colors = [WARN if row["congested"] else ACCENT for _, row in hourly.iterrows()]
    ax3.bar(hourly["hour"], hourly["mean"], color=bar_colors,
            edgecolor=GRID_C, linewidth=0.5, width=0.8)
    ax3.axhline(stats["p25"], color=WARN, lw=1.2, linestyle="--",
                label=f"Congestion P25 = {stats['p25']:.1f} Mbps")
    ax3.set_xlabel("Hour of Day (24h)"); ax3.set_ylabel("Avg Speed (Mbps)")
    ax3.xaxis.set_major_locator(ticker.MultipleLocator(1))
    ax3.legend(fontsize=7.5, facecolor=BG_AX, edgecolor=GRID_C, labelcolor=TEXT_C)
    bh = hourly[hourly["hour"] == stats["busiest_hour"]].iloc[0]
    fh = hourly[hourly["hour"] == stats["fastest_hour"]].iloc[0]
    offset = max(stats["overall_std"] * 0.4, 0.5)
    ax3.annotate(f"Slowest\n{stats['busiest_hour']:02d}:00",
                 xy=(stats["busiest_hour"], bh["mean"]),
                 xytext=(stats["busiest_hour"], bh["mean"] + offset),
                 color=WARN, fontsize=7, ha="center",
                 arrowprops=dict(arrowstyle="->", color=WARN, lw=0.8))
    ax3.annotate(f"Fastest\n{stats['fastest_hour']:02d}:00",
                 xy=(stats["fastest_hour"], fh["mean"]),
                 xytext=(stats["fastest_hour"], fh["mean"] + offset),
                 color=SUCCESS_C, fontsize=7, ha="center",
                 arrowprops=dict(arrowstyle="->", color=SUCCESS_C, lw=0.8))

    # 4 — Packet loss per run (colour-coded bars)
    _style_ax(ax4, "Packet Loss % per Run")
    loss_colors = [WARN if v > 5 else (PARTIAL_C if v > 0 else SUCCESS_C) for v in pl_vals]
    ax4.bar(runs, pl_vals, color=loss_colors, edgecolor=GRID_C, linewidth=0.4, width=0.8)
    ax4.axhline(5.0, color=WARN, lw=1.2, linestyle="--", label="Warning threshold (5%)")
    ax4.axhline(stats["avg_packet_loss"], color="#ffcc00", lw=1.1, linestyle=":",
                label=f"Avg {stats['avg_packet_loss']:.1f}%")
    ax4.set_xlabel("Run #"); ax4.set_ylabel("Packet Loss (%)")
    ax4.set_ylim(0, max(float(np.max(pl_vals)) * 1.3, 12))
    ax4.legend(handles=[Patch(color=WARN,      label=">5%  High loss"),
                        Patch(color=PARTIAL_C, label="1-5% Moderate"),
                        Patch(color=SUCCESS_C, label="0%   No loss")],
               fontsize=7.5, facecolor=BG_AX, edgecolor=GRID_C, labelcolor=TEXT_C)

    # 5 — Speed distribution histogram with mean / median / P25 lines
    _style_ax(ax5, "Speed Distribution (SUCCESS runs only)")
    ax5.hist(success["speed_Mbps"], bins=min(20, len(success)),
             color=ACCENT, edgecolor="#000020", alpha=0.85)
    ax5.axvline(stats["overall_mean"],   color="#ffcc00", lw=1.4, linestyle="--",
                label=f"Mean   {stats['overall_mean']:.1f} Mbps")
    ax5.axvline(stats["overall_median"], color=SUCCESS_C, lw=1.4, linestyle=":",
                label=f"Median {stats['overall_median']:.1f} Mbps")
    ax5.axvline(stats["p25"],            color=WARN,      lw=1.2, linestyle="-.",
                label=f"P25    {stats['p25']:.1f} Mbps")
    ax5.set_xlabel("Speed (Mbps)"); ax5.set_ylabel("Frequency")
    ax5.legend(fontsize=7.5, facecolor=BG_AX, edgecolor=GRID_C, labelcolor=TEXT_C)

    # 6 — Duration (filled area) + Latency (dual y-axis)
    _style_ax(ax6, "Latency & Duration per Run")
    ax6.fill_between(runs, dur_vals, alpha=0.25, color=ACCENT)
    ax6.plot(runs, dur_vals, color=ACCENT, lw=1.4, marker="s",
             markersize=3.5, label="Duration (s)")
    ax6.axhline(stats["avg_duration"], color="#ffcc00", lw=1.0, linestyle=":",
                label=f"Avg dur {stats['avg_duration']:.1f}s")
    ax6.set_xlabel("Run #"); ax6.set_ylabel("Duration (s)")
    ax6r = ax6.twinx()
    ax6r.plot(runs, lat_vals, color=LAT_C, lw=1.3, marker="^",
              markersize=3.5, linestyle="--", label="Latency (ms)")
    ax6r.set_ylabel("Latency (ms)", color=LAT_C, fontsize=9)
    ax6r.tick_params(colors=LAT_C, labelsize=8)
    ax6r.spines["right"].set_edgecolor(LAT_C)
    l1, lb1 = ax6.get_legend_handles_labels()
    l2, lb2 = ax6r.get_legend_handles_labels()
    ax6.legend(l1 + l2, lb1 + lb2,
               fontsize=7.5, facecolor=BG_AX, edgecolor=GRID_C, labelcolor=TEXT_C)


# ── Chart builders ────────────────────────────────────────────────────────────

def build_charts(df: pd.DataFrame, stats: dict, output_prefix: str, out_dir: str = "results") -> str:
    """Build a combined 6-panel chart + status pie and save as one PNG."""
    fig = plt.figure(figsize=(20, 18), facecolor=BG_FIG)
    # 4-row grid: rows 0-2 hold the 6 data panels (2 per row), row 3 = centred pie
    gs = GridSpec(4, 2, figure=fig, hspace=0.50, wspace=0.38,
                  height_ratios=[1, 1, 1, 0.35])

    success = stats["df_success"].copy().reset_index(drop=True)
    runs    = (success["run_number"].values if "run_number" in success.columns
               else np.arange(1, len(success) + 1))

    axes = [fig.add_subplot(gs[r, c]) for r in range(3) for c in range(2)]
    _draw_panels(axes, success, runs, stats)

    # Status pie (centred in row 3)
    ax7 = fig.add_subplot(gs[3, :])
    ax7.set_facecolor(BG_FIG)
    pie_data = [(stats["n_success"], "SUCCESS", SUCCESS_C),
                (stats["n_partial"], "PARTIAL", PARTIAL_C),
                (stats["n_skipped"], "SKIPPED", SKIP_C)]
    pie_data = [(v, l, c) for v, l, c in pie_data if v > 0]
    wedges, texts, autos = ax7.pie(
        [x[0] for x in pie_data],
        labels=[f"{x[1]} ({x[0]})" for x in pie_data],
        colors=[x[2] for x in pie_data],
        autopct="%1.0f%%", startangle=90,
        textprops={"color": TEXT_C, "fontsize": 11},
    )
    for at in autos:
        at.set_color("#0f0f1a"); at.set_fontsize(10)
    ax7.set_title("Run Status Breakdown", color=TEXT_C, fontsize=13,
                  fontweight="bold", pad=12)

    fig.suptitle("Network Download Analysis", color=TEXT_C, fontsize=15,
                 fontweight="bold", y=0.995)
    fig.tight_layout()

    out_path = str(Path(out_dir) / f"{output_prefix}_charts.png")
    fig.savefig(out_path, dpi=150, facecolor=BG_FIG, bbox_inches="tight")
    plt.close(fig)
    return out_path


def save_individual_charts(df: pd.DataFrame, stats: dict, out_dir: str) -> list:
    """Save each panel as its own PNG file."""
    success = stats["df_success"].copy().reset_index(drop=True)
    runs    = (success["run_number"].values if "run_number" in success.columns
               else np.arange(1, len(success) + 1))

    saved = []

    # Panels 1-6: create a fresh figure per panel, reuse _draw_panels logic
    panel_titles = [
        "Download Speed per Run",
        "Speed vs Packet Loss per Run",
        "Average Speed by Hour of Day",
        "Packet Loss % per Run",
        "Speed Distribution (SUCCESS runs only)",
        "Latency & Duration per Run",
    ]
    figs_axes = []
    for title in panel_titles:
        f, ax = plt.subplots(figsize=(10, 5), facecolor=BG_FIG)
        figs_axes.append((f, ax))

    _draw_panels([fa[1] for fa in figs_axes], success, runs, stats)

    file_names = ["01_speed_per_run", "02_speed_vs_packet_loss", "03_hourly_avg_speed",
                  "04_packet_loss", "05_speed_distribution", "06_latency_duration"]
    for (f, ax), name in zip(figs_axes, file_names):
        f.tight_layout()
        p = str(Path(out_dir) / f"{name}.png")
        f.savefig(p, dpi=150, facecolor=BG_FIG); plt.close(f)
        saved.append(p)

    # Panel 7: status pie
    f, ax = plt.subplots(figsize=(6, 6), facecolor=BG_FIG)
    ax.set_facecolor(BG_FIG)
    pie_data = [(stats["n_success"], "SUCCESS", SUCCESS_C),
                (stats["n_partial"], "PARTIAL", PARTIAL_C),
                (stats["n_skipped"], "SKIPPED", SKIP_C)]
    pie_data = [(v, l, c) for v, l, c in pie_data if v > 0]
    wedges, texts, autos = ax.pie(
        [x[0] for x in pie_data],
        labels=[f"{x[1]} ({x[0]})" for x in pie_data],
        colors=[x[2] for x in pie_data],
        autopct="%1.0f%%", startangle=90,
        textprops={"color": TEXT_C, "fontsize": 11},
    )
    for at in autos:
        at.set_color("#0f0f1a"); at.set_fontsize(10)
    ax.set_title("Run Status Breakdown", color=TEXT_C, fontsize=13,
                 fontweight="bold", pad=12)
    f.tight_layout()
    p = str(Path(out_dir) / "07_status_breakdown.png")
    f.savefig(p, dpi=150, facecolor=BG_FIG); plt.close(f)
    saved.append(p)

    return saved


# ── Report & console output ───────────────────────────────────────────────────

def write_report(df: pd.DataFrame, stats: dict, output_prefix: str, out_dir: str = "results") -> str:
    """Generate a human-readable .txt report and save it."""
    hourly   = stats["hourly"]
    slope_s  = "improving (speeds rising)" if stats["slope"] > 0 else "degrading (speeds falling)"
    sig      = ("statistically significant" if stats["p_value"] < 0.05
                else "not statistically significant")

    congested_hours = hourly[hourly["congested"]]["hour"].tolist()
    congested_str   = (", ".join(f"{h:02d}:00" for h in congested_hours)
                       if congested_hours else "None detected")

    # Packet-loss verdict
    avg_loss = stats["avg_packet_loss"]
    if avg_loss > 10:
        loss_verdict = f"SEVERE packet loss ({avg_loss:.1f}% avg) -- likely congestion or faulty link."
    elif avg_loss > 5:
        loss_verdict = f"MODERATE packet loss ({avg_loss:.1f}% avg) -- network under stress."
    elif avg_loss > 0:
        loss_verdict = f"LOW packet loss ({avg_loss:.1f}% avg) -- mostly healthy."
    else:
        loss_verdict = "No packet loss detected -- network link is clean."

    # Speed diagnosis: distinguish congestion vs ISP throttling
    if stats["congestion_pct"] > 10:
        speed_diagnosis = (f"Network congestion likely (high-loss + low-speed = "
                           f"{stats['congestion_pct']:.1f}% of runs).")
    elif stats["throttle_pct"] > 10:
        speed_diagnosis = (f"ISP throttling suspected (low-loss + low-speed = "
                           f"{stats['throttle_pct']:.1f}% of runs).")
    else:
        speed_diagnosis = "No strong congestion or throttling pattern detected."

    bh_speed   = hourly[hourly["hour"] == stats["busiest_hour"]]["mean"].values[0]
    fh_speed   = hourly[hourly["hour"] == stats["fastest_hour"]]["mean"].values[0]
    improvement = fh_speed / max(bh_speed, 0.01)

    table_lines = [f"  {'Hour':>5}  {'Mean Mbps':>10}  {'Median':>8}  {'Std':>7}  "
                   f"{'Min':>7}  {'Max':>7}  {'Samples':>8}", "  " + "-" * 67]
    for _, row in hourly.iterrows():
        flag = (" << SLOWEST" if int(row["hour"]) == stats["busiest_hour"] else
                " << FASTEST" if int(row["hour"]) == stats["fastest_hour"] else "")
        table_lines.append(
            f"  {int(row['hour']):>5}  {row['mean']:>10.2f}  {row['median']:>8.2f}"
            f"  {row.get('std', 0.0):>7.2f}  {row['min']:>7.2f}"
            f"  {row['max']:>7.2f}  {int(row['count']):>8}{flag}"
        )

    report = f"""\
================================================================================
  NETWORK DOWNLOAD SPEED ANALYSIS REPORT
  Samples : {stats['total_runs']} total  ({stats['n_success']} SUCCESS | {stats['n_partial']} PARTIAL | {stats['n_skipped']} SKIPPED)
================================================================================

1. OVERALL SUMMARY
  Mean / Median speed  : {stats['overall_mean']:.2f} / {stats['overall_median']:.2f} Mbps
  Std / CV             : {stats['overall_std']:.2f} Mbps  ({stats['cv']:.1f}%)
  Min / Max speed      : {stats['overall_min']:.2f} / {stats['overall_max']:.2f} Mbps
  Avg download time    : {stats['avg_duration']:.1f} s

2. PACKET LOSS
  Average / Max        : {stats['avg_packet_loss']:.2f}% / {stats['max_packet_loss']:.2f}%
  Verdict              : {loss_verdict}
  Congestion events    : {stats['congestion_pct']:.1f}%  (high-loss + low-speed)
  Throttle events      : {stats['throttle_pct']:.1f}%   (low-loss + low-speed)
  Speed diagnosis      : {speed_diagnosis}

3. LATENCY
  Avg / Min / Max      : {stats['avg_latency']:.2f} / {stats['min_latency']:.2f} / {stats['max_latency']:.2f} ms

4. CONGESTION
  Threshold (P25)      : {stats['p25']:.2f} Mbps
  Congested hours      : {congested_str}
  Slowest hour         : {stats['busiest_hour']:02d}:00  (avg {bh_speed:.2f} Mbps)
  Fastest hour         : {stats['fastest_hour']:02d}:00  (avg {fh_speed:.2f} Mbps)
  Improvement factor   : {improvement:.2f}x

5. TREND (linear regression on SUCCESS runs)
  Slope                : {stats['slope']:+.4f} Mbps/run  ({slope_s})
  Pearson r / p-value  : {stats['r_value']:.4f} / {stats['p_value']:.4f}  ({sig})

6. HOURLY BREAKDOWN
{chr(10).join(table_lines)}

7. RECOMMENDATIONS
  Best time for transfers  : {stats['fastest_hour']:02d}:00  ({fh_speed:.2f} Mbps avg)
  Worst time for transfers : {stats['busiest_hour']:02d}:00  ({bh_speed:.2f} Mbps avg)
  Packet loss verdict      : {"Contact ISP -- loss >5% degrades TCP." if stats['avg_packet_loss'] > 5 else "Within acceptable range (<5%)."}
  Variability (CV)         : {"HIGH -- check for throttling/contention." if stats['cv'] > 30 else "MODERATE -- network is stable."}
  Outages                  : {stats['n_skipped']} skipped run(s) out of {stats['total_runs']}.  {"No outages." if stats['n_skipped'] == 0 else "Check router logs or ISP status."}

================================================================================
Generated by network_analyzer.py
================================================================================
"""
    report_path = str(Path(out_dir) / "report.txt")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    return report_path


def print_summary(stats: dict) -> None:
    """Print a quick stats summary to the console."""
    print("\n" + "=" * 60)
    print("  NETWORK ANALYSIS SUMMARY")
    print("=" * 60)
    print(f"  Total runs    : {stats['total_runs']}  "
          f"(OK: {stats['n_success']}  Partial: {stats['n_partial']}  Skipped: {stats['n_skipped']})")
    print(f"  Mean speed    : {stats['overall_mean']:.2f} Mbps")
    print(f"  Median speed  : {stats['overall_median']:.2f} Mbps")
    print(f"  Std / CV      : {stats['overall_std']:.2f} Mbps  ({stats['cv']:.1f}%)")
    print(f"  Min / Max     : {stats['overall_min']:.2f} / {stats['overall_max']:.2f} Mbps")
    print(f"  Avg latency   : {stats['avg_latency']:.2f} ms  (min {stats['min_latency']:.0f} / max {stats['max_latency']:.0f} ms)")
    print(f"  Packet loss   : {stats['avg_packet_loss']:.1f}% avg  /  {stats['max_packet_loss']:.1f}% peak")
    print(f"  Slowest hour  : {stats['busiest_hour']:02d}:00")
    print(f"  Fastest hour  : {stats['fastest_hour']:02d}:00")
    print("=" * 60 + "\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyse network download CSV logs and generate charts + report.")
    parser.add_argument("csv_files", nargs="+", metavar="CSV",
                        help="One or more CSV files from network_downloader.py.")
    parser.add_argument("--output", default="network_analysis", metavar="PREFIX",
                        help="Output file prefix (default: network_analysis).")
    parser.add_argument("--results-dir", default="results", metavar="DIR",
                        help="Folder to save all outputs (default: results/).")
    args = parser.parse_args()

    Path(args.results_dir).mkdir(parents=True, exist_ok=True)
    print(f"\nOutput folder: {args.results_dir}/")

    print(f"Loading {len(args.csv_files)} CSV file(s)...")
    df = load_all_csvs(args.csv_files)
    print(f"  -> {len(df)} total records loaded.\n")

    stats = compute_stats(df)
    print_summary(stats)

    combined_path = build_charts(df, stats, args.output, out_dir=args.results_dir)
    print(f"  Combined chart   -> {combined_path}")

    individual_paths = save_individual_charts(df, stats, args.results_dir)
    for p in individual_paths:
        print(f"  Individual chart -> {p}")

    report_path = write_report(df, stats, args.output, out_dir=args.results_dir)
    print(f"  Report           -> {report_path}")

    print(f"\nAll {len(individual_paths) + 1} charts + report saved to {args.results_dir}/\n")


if __name__ == "__main__":
    main()