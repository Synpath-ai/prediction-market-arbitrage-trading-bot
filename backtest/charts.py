"""Charts of a backtest (out/backtest.json from backtest/backtest.py).

    python -m backtest.charts                                        # every market, whole backtest
    python -m backtest.charts --market kalshi:<TICKER> --start YYYY-MM-DD --end YYYY-MM-DD
    python -m backtest.charts --market kalshi:<TICKER> --start ... --end ... --by-close
    python -m backtest.charts --best                                 # each market's best 4-6 trades

For each market, two charts of the same trades:
  out/entries_<ticker>[_<period>].png   both venues' prices, the spread between them shaded, every
                                        ENTRY n tagged with the edge it locked and every EXIT n
  out/pnl_<ticker>[_<period>].png       total and period, the running P&L, and each trade as bar #n
and, without --market, out/pnl_all[_<period>].png for the markets together. A period keeps the
trades that opened and closed inside it, or with --by-close the trades that closed inside it (P&L
counted when realized; the footnote names any that opened earlier). The footnote always gives the
whole backtest's total.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Polygon
from matplotlib.text import Text

OUT = Path("out")
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
BG, TEXT, MUTED, GRID, EDGE = "#0f1216", "#e6e8eb", "#8b929b", "#222831", "#2d343d"
KALSHI, POLY, SPREAD, EXIT, TAG = "#38bdf8", "#f5b83d", "#a78bfa", "#fb7185", "#1a1f26"
LINE, GAIN, LOSS = "#a78bfa", "#a78bfa", "#fb7185"
MONO = "DejaVu Sans Mono"
VENUE = {"kalshi": "Kalshi", "polymarket": "Poly"}


def dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def day_ms(text: str, *, end: bool = False) -> int:
    """A date as epoch ms: its first hour, or with `end` its last."""
    d = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000) + (DAY_MS - HOUR_MS if end else 0)


def money(x: float) -> str:
    return ("+" if x >= 0 else "−") + f"${abs(x):,.2f}"


def mid(bid, ask):
    if bid is None and ask is None:
        return None
    return ((bid if bid is not None else ask) + (ask if ask is not None else bid)) / 2


def period_text(start: int, end: int) -> str:
    a, b = dt(start), dt(end)
    return f"{a:%b %d} – {b:%d}, {b:%Y}" if a.month == b.month else f"{a:%b %d} – {b:%b %d}, {b:%Y}"


def best_window(trades: list[dict], lo: int = 4, hi: int = 6) -> tuple[int, int]:
    """(start, length) of the best run of closed trades: no loser if any run has none, then the
    highest total."""
    best = None
    for n in range(lo, min(hi, len(trades)) + 1):
        for i in range(len(trades) - n + 1):
            run = trades[i:i + n]
            key = (all(t["pnl"] > 0 for t in run), sum(t["pnl"] for t in run), -n)
            if best is None or key > best[0]:
                best = (key, i, n)
    return (best[1], best[2]) if best else (0, len(trades))


def style_axes(ax, *, spines=("left", "bottom")) -> None:
    ax.set_facecolor(BG)
    ax.set_axisbelow(True)
    for name, side in ax.spines.items():
        side.set_visible(name in spines)
        side.set_color(EDGE)


def place(fig, ax, items: list, taken: list, *, pad: float = 7) -> None:
    """Put each (text, xy, candidate offsets, style) at the first offset whose box stays inside
    the axes and clear of everything placed so far; the last candidate is kept if none is clear."""
    renderer = fig.canvas.get_renderer()
    frame = ax.get_window_extent(renderer)
    grow = pad * fig.dpi / 72
    for text, xy, spots, style in items:
        for i, (dx, dy, ha, va) in enumerate(spots):
            ann = ax.annotate(text, xy=xy, xytext=(dx, dy), textcoords="offset points", ha=ha, va=va, **style)
            ann.update_positions(renderer)                 # apply the offset before measuring
            w = Text.get_window_extent(ann, renderer)      # the text alone, not its connector
            box = (w.x0 - grow, w.y0 - grow, w.x1 + grow, w.y1 + grow)
            inside = box[0] >= frame.x0 and box[2] <= frame.x1 and box[1] >= frame.y0 and box[3] <= frame.y1
            free = not any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3] for o in taken)
            if (inside and free) or i == len(spots) - 1:
                taken.append(box)
                break
            ann.remove()


def point_boxes(ax, points: list, r: float = 9) -> list:
    out = []
    for x, y in points:
        px, py = ax.transData.transform((mdates.date2num(x), y))
        out.append((px - r, py - r, px + r, py + r))
    return out


# -- entries and exits ----------------------------------------------------------

def entries_chart(pair: dict, trades: list[dict], start: int, end: int, path: Path, *, strategy: str,
                  source: str, note: str) -> None:
    rows = [r for r in pair["series"] if start <= r[0] <= end]
    t = [dt(r[0]) for r in rows]
    k = [mid(r[1], r[2]) * 100 for r in rows]
    p = [r[3] * 100 for r in rows]
    at = {r[0]: i for i, r in enumerate(rows)}
    days = (end - start) / DAY_MS
    small = days > 30

    fig, ax = plt.subplots(figsize=(14, 7.2))
    fig.patch.set_facecolor(BG)
    style_axes(ax)
    ax.fill_between(t, k, p, step="post", color=SPREAD, alpha=0.26, linewidth=0, label="Spread")
    ax.step(t, k, where="post", color=KALSHI, lw=1.7, label="Kalshi")
    ax.step(t, p, where="post", color=POLY, lw=1.7, ls=(0, (4, 2)), label="Polymarket")
    lo, hi = min(k + p), max(k + p)
    pad = max(2.0, (hi - lo) * 0.12)
    ax.set_ylim(max(0, lo - pad * 1.6), min(100, hi + pad * 2.4))
    ax.set_xlim(t[0], t[-1])

    entry_style = dict(fontsize=7 if small else 8.8, fontfamily=MONO, fontweight="bold", color=TEXT, zorder=6,
                       bbox=dict(boxstyle="square,pad=0.4", fc=TAG, ec=TEXT, lw=0.8),
                       arrowprops=dict(arrowstyle="-", color=TEXT, lw=0.9, shrinkB=6))
    exit_style = dict(fontsize=7 if small else 8.5, fontfamily=MONO, fontweight="bold", color=EXIT, zorder=6,
                      bbox=dict(boxstyle="square,pad=0.35", fc=TAG, ec=EXIT, lw=0.8),
                      arrowprops=dict(arrowstyle="-", color=EXIT, lw=0.9, shrinkB=5))
    held_style = {**exit_style, "color": MUTED, "bbox": dict(boxstyle="square,pad=0.35", fc=TAG, ec=MUTED, lw=0.8, ls="--"),
                  "arrowprops": dict(arrowstyle="-", color=MUTED, lw=0.9, ls="--", shrinkB=5)}
    entry_spots = [(dx, dy, "center", "bottom") for dy in (30, 58, 86, 114, 142, 170)
                   for dx in (0, -70, 70, -140, 140)] + [(0, -34, "center", "top"), (-70, -34, "center", "top")]
    exit_spots = [(sx * d, sy * d, "left" if sx > 0 else "right", "top" if sy < 0 else "bottom")
                  for d in (26, 50, 76, 104) for sy in (-1, 1) for sx in (1, -1)]
    items, points = [], []
    for n, tr in enumerate(trades, 1):
        e = at[tr["entry_ms"]]
        top = max(k[e], p[e])
        ax.plot(t[e], top, marker="v", color=TEXT, ms=6 if small else 8, mec=BG, mew=0.8, zorder=7)
        points.append((t[e], top))
        items.append((f"ENTRY {n} · +{tr['edge'] * 100:.1f}%", (t[e], top), entry_spots, entry_style))
    for n, tr in enumerate(trades, 1):
        if tr["exit_ms"] is None:            # still held when the data ends
            x = len(t) - 1
            y = k[x] if tr["yes_venue"] == "kalshi" else p[x]
            points.append((t[x], y))
            items.append((f"OPEN {n} · held", (t[x], y), [(-dx, dy, "right", va) for dx, dy, _, va in exit_spots if dx > 0],
                          held_style))
            continue
        x = at[tr["exit_ms"]]
        y = k[x] if tr["yes_venue"] == "kalshi" else p[x]
        ax.plot(t[x], y, "o", mfc=BG, mec=EXIT, mew=2, ms=6.5 if small else 8.5, zorder=7)
        points.append((t[x], y))
        items.append((f"EXIT {n}", (t[x], y), exit_spots, exit_style))

    ax.set_ylabel("Price (¢)", fontsize=11, color=MUTED)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}¢")
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, round(days / 10))))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.grid(color=GRID, lw=0.8)
    ax.tick_params(colors=MUTED, labelsize=9.5)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_fontfamily(MONO)
    leg = ax.legend(loc="upper left", fontsize=9.5, frameon=False, ncol=3, handlelength=2.2)
    for txt in leg.get_texts():
        txt.set_color(TEXT)
    fig.text(0.012, 0.965, pair["name"], fontsize=15, fontweight="bold", color=TEXT, va="top")
    fig.text(0.012, 0.922, f"Kalshi × Polymarket  ·  {strategy}  ·  data via synpath", fontsize=10, color=MUTED, va="top")
    fig.text(0.988, 0.012, f"{pair['kalshi_id']} / {pair['poly_id']}  ·  hourly, Polymarket {source}  ·  {note}  ·  "
             "entry % = edge locked after fees per $1", ha="right", fontsize=7.8, color=MUTED, fontfamily=MONO)
    fig.tight_layout(rect=(0, 0.025, 1, 0.9))
    place(fig, ax, items, point_boxes(ax, points, r=7 * fig.dpi / 72))
    fig.savefig(path, dpi=160, facecolor=BG)
    plt.close(fig)
    print(path)


# -- P&L ------------------------------------------------------------------------

def gradient_fill(ax, x, y, color: str) -> None:
    xnum = mdates.date2num(x)
    top = max(y) if max(y) > 0 else 1
    img = np.zeros((256, 1, 4))
    img[:, :, :3] = matplotlib.colors.to_rgb(color)
    img[:, :, 3] = np.linspace(0.0, 0.38, 256)[:, None]
    im = ax.imshow(img, aspect="auto", origin="lower", extent=(xnum[0], xnum[-1], 0, top), zorder=1)
    poly = Polygon(np.column_stack([np.r_[xnum[0], xnum, xnum[-1]], np.r_[0, y, 0]]), closed=True,
                   facecolor="none", edgecolor="none", transform=ax.transData)
    ax.add_patch(poly)
    im.set_clip_path(poly)


def peak_capital(trades: list[dict], contracts: float) -> float:
    """The most money the trades had tied up at once: each position costs what both legs and
    their entry fees cost, from its entry until its exit."""
    events = []
    for t in trades:
        outlay = (t["cost"] + t["fees"]) * contracts
        events += [(t["entry_ms"], outlay), (t["exit_ms"] if t["exit_ms"] is not None else float("inf"), -outlay)]
    peak = level = 0.0
    for _, change in sorted(events, key=lambda e: (e[0], e[1])):     # a same-hour exit frees money first
        level += change
        peak = max(peak, level)
    return peak


def pnl_chart(title: str, trades: list[dict], contracts: float, start: int, end: int, path: Path, *,
              strategy: str, footnote: str, numbered: bool) -> None:
    trades = sorted(trades, key=lambda t: t["exit_ms"])
    pnl = [t["pnl"] * contracts for t in trades]
    total = sum(pnl)
    wins = sum(v > 0 for v in pnl)
    days = (dt(end).date() - dt(start).date()).days + 1

    fig = plt.figure(figsize=(14, 8.6))
    fig.patch.set_facecolor(BG)
    grid = fig.add_gridspec(2, 1, height_ratios=[1.7, 1], hspace=0.42, left=0.075, right=0.975, top=0.70, bottom=0.12)
    ax, bx = fig.add_subplot(grid[0]), fig.add_subplot(grid[1])
    fig.text(0.02, 0.955, title, fontsize=15, fontweight="bold", color=TEXT, va="top")
    fig.text(0.02, 0.915, f"Kalshi × Polymarket  ·  {strategy}  ·  data via synpath", fontsize=10, color=MUTED, va="top")
    capital = peak_capital(trades, contracts)
    ret = total / capital if capital else None
    stats = [("TOTAL P&L", money(total), GAIN if total >= 0 else LOSS, f"{contracts:,.0f} contracts per leg"),
             ("RETURN", "—" if ret is None else f"{'+' if ret >= 0 else '−'}{abs(ret) * 100:.1f}%",
              GAIN if (ret or 0) >= 0 else LOSS, f"on ${capital:,.0f} peak capital"),
             ("PERIOD", period_text(start, end), TEXT, f"{days} days"),
             ("TRADES", f"{len(trades)}", TEXT, f"{wins} won · {len(trades) - wins} lost"),
             ("PER TRADE", money(total / len(trades)) if trades else "—", TEXT, "average round trip")]
    xs_stats = (0.02, 0.2, 0.36, 0.62, 0.78)
    for i, (k, v, color, note) in enumerate(stats):
        x = xs_stats[i]
        fig.text(x, 0.855, k, fontsize=8.5, color=MUTED, fontfamily=MONO, va="top")
        fig.text(x, 0.83, v, fontsize=22 if i < 2 else 17, fontweight="bold", color=color, fontfamily=MONO, va="top")
        fig.text(x, 0.772, note, fontsize=8.5, color=MUTED, va="top")

    # Running P&L: $0 when the period starts, a point at every exit, flat to the period's end.
    xs = [dt(start)] + [dt(t["exit_ms"]) for t in trades] + [dt(end)]
    ys = list(np.cumsum([0.0] + pnl)) + [total]
    gradient_fill(ax, xs, ys, LINE)
    ax.plot(xs, ys, color=LINE, lw=2.6, solid_capstyle="round", zorder=3)
    ax.plot(xs[1:-2], ys[1:-2], "o", ms=7, color=BG, mec=LINE, mew=2, zorder=4)
    if trades:
        ax.plot(xs[-2], ys[-2], "o", ms=10, color=LINE, mec=BG, mew=2, zorder=5)
        ax.plot(xs[-2], ys[-2], "o", ms=20, color=LINE, alpha=0.18, zorder=4)
    top = max(ys) if max(ys) > 0 else 1
    ax.set_ylim(min(0, min(ys)) - top * 0.06, top * 1.22)
    span = (xs[-1] - xs[0]).total_seconds()
    ax.set_xlim(xs[0] - timedelta(seconds=span * 0.02), xs[-1] + timedelta(seconds=span * 0.02))
    ax.set_title("CUMULATIVE P&L", loc="left", fontsize=8.5, color=MUTED, fontfamily=MONO, pad=10)
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, show_offset=False))

    idx = np.arange(len(trades))
    many = len(trades) > 8
    bx.bar(idx, pnl, width=0.52, color=[GAIN if v > 0 else LOSS for v in pnl], zorder=3)
    for i, v in enumerate(pnl):
        bx.annotate(money(v) if not many else f"{'+' if v >= 0 else '−'}${abs(v):,.0f}", xy=(i, v),
                    xytext=(0, 5 if v >= 0 else -5), textcoords="offset points", ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=8 if many else 10, fontweight="bold",
                    fontfamily=MONO, color=TEXT)
    labels = []
    for n, t in enumerate(trades, 1):
        market = t.get("market", "")
        if many:
            short = market if market.startswith("Market ") else market.split()[0]
            labels.append(f"{short}\n{dt(t['exit_ms']):%b %d}")
            continue
        held = (t["exit_ms"] - t["entry_ms"]) / HOUR_MS
        head = f"#{n}  ·  {market}" if numbered else market
        labels.append(f"{head}\n{dt(t['exit_ms']):%b %d %H:%M}  ·  held {held:.0f}h\n"
                      f"YES {VENUE[t['yes_venue']]} {t['yes_price'] * 100:.1f}¢ + NO {VENUE[t['no_venue']]} {t['no_price'] * 100:.1f}¢")
    bx.set_xticks(idx, labels)
    bx.axhline(0, color=EDGE, lw=1)
    hi_, lo_ = max(pnl + [1]), min(pnl + [0])
    bx.set_ylim(lo_ * 1.9 - hi_ * 0.08, hi_ * 1.3)
    bx.set_xlim(-0.6, max(len(trades), 1) - 0.4)
    bx.set_title("P&L PER TRADE", loc="left", fontsize=8.5, color=MUTED, fontfamily=MONO, pad=10)

    for a in (ax, bx):
        style_axes(a, spines=("bottom",))
        a.grid(axis="y", color=GRID, lw=0.9)
        a.tick_params(colors=MUTED, labelsize=8.5, length=0)
        a.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
        for lbl in a.get_yticklabels():
            lbl.set_fontfamily(MONO)
    for lbl in bx.get_xticklabels():
        lbl.set_fontsize(8)
        lbl.set_color(MUTED)
        lbl.set_linespacing(1.5)

    # Running totals: the last one first and above its point, the others wherever they fit.
    spots = [(0, 13, "center", "bottom"), (0, -15, "center", "top"), (-12, 12, "right", "bottom"),
             (12, -14, "left", "top"), (14, 0, "left", "center"), (-14, 0, "right", "center"),
             (0, 34, "center", "bottom"), (0, -36, "center", "top")]
    plain = dict(fontsize=9.5, fontweight="bold", fontfamily=MONO, color=MUTED, zorder=6)
    items = [("$0", (xs[0], 0), [(0, 12, "center", "bottom")], {**plain, "fontweight": "normal"})]
    if trades:
        items.insert(0, (money(total), (xs[-2], ys[-2]), [(-10, 13, "right", "bottom")],
                         {**plain, "fontsize": 12, "color": TEXT}))
        if not many:
            items += [(money(y), (x, y), spots, plain) for x, y in zip(xs[1:-2], ys[1:-2])]
    place(fig, ax, items, point_boxes(ax, list(zip(xs[:-1], ys[:-1]))), pad=2)
    if len(footnote) > 150:          # too long for one line: the caveats go on a second
        head, _, tail = footnote.partition("  ·  hourly backtest")
        footnote = f"{head}\nhourly backtest{tail}" if tail else footnote
    fig.text(0.975, 0.012, footnote, ha="right", va="bottom", fontsize=7.8, color=MUTED, fontfamily=MONO,
             linespacing=1.6)
    fig.savefig(path, dpi=160, facecolor=BG)
    plt.close(fig)
    print(path)


# -- command line ---------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="out/backtest.json")
    ap.add_argument("--market", help="one market's Kalshi id (default: every market)")
    ap.add_argument("--start", help="first day, YYYY-MM-DD (UTC)")
    ap.add_argument("--end", help="last day, YYYY-MM-DD (UTC)")
    ap.add_argument("--best", action="store_true", help="each market's best run of 4-6 consecutive trades")
    ap.add_argument("--generic-names", action="store_true",
                    help="label markets Market A, B, C... instead of by name (for sharing)")
    ap.add_argument("--by-close", action="store_true",
                    help="a period counts the trades that closed in it (P&L counted when realized), "
                         "even one opened before it; the price chart starts early enough to show its entry")
    a = ap.parse_args()

    data = json.loads(Path(a.file).read_text())
    contracts, strategy, source = data["contracts"], data["strategy"], data["source"]
    pairs = [p for p in data["pairs"] if a.market is None or p["kalshi_id"] == a.market]
    if not pairs:
        raise SystemExit(f"{a.market} is not in {a.file}")
    OUT.mkdir(exist_ok=True)
    tag = f"_{a.start}_{a.end}" if a.start or a.end else ("_best" if a.best else "")
    caveat = "hourly backtest, Polymarket at its quoted price, fills assumed at the quoted prices"
    together = []
    if a.generic_names:
        for i, pair in enumerate(pairs):
            letter = chr(ord("A") + i)
            pair["event"] = pair["name"] = f"Market {letter}"
    for pair in pairs:
        series_start, series_end = pair["series"][0][0], pair["series"][-1][0]
        start = day_ms(a.start) if a.start else series_start
        end = day_ms(a.end, end=True) if a.end else series_end
        start, end = max(start, series_start), min(end, series_end)
        closed = [t for t in pair["trades"] if t["exit_ms"] is not None]
        whole = sum(t["pnl"] for t in closed) * contracts
        if a.best:
            i, n = best_window(closed)
            shown = closed[i:i + n]
            start = max(series_start, min(t["entry_ms"] for t in shown) - 12 * HOUR_MS)
            end = min(series_end, max(t["exit_ms"] for t in shown) + 12 * HOUR_MS)
            where = f"trades {i + 1}–{i + n} of {len(closed)}"
        elif a.by_close:
            shown = [t for t in closed if start <= t["exit_ms"] <= end]
            early = [n for n, t in enumerate(shown, 1) if t["entry_ms"] < start]
            where = (f"{len(shown)} of {len(pair['trades'])} trades, those closed in the period (P&L counted when realized)"
                     + "".join(f"; #{n} opened {dt(shown[n - 1]['entry_ms']):%b %d}" for n in early))
        else:
            # Trades that opened and closed inside the period; one still held counts if it opened inside.
            shown = [t for t in pair["trades"] if start <= t["entry_ms"] and (t["exit_ms"] or series_end) <= end]
            where = f"{len(shown)} of {len(pair['trades'])} trades, those opened and closed in the period"
        ticker = pair["kalshi_id"].split(":", 1)[1]
        # The price chart reaches back to the earliest entry it shows.
        chart_start = max(series_start, min([start] + [t["entry_ms"] - 12 * HOUR_MS for t in shown]))
        entries_chart(pair, shown, chart_start, end, OUT / f"entries_{ticker}{tag}.png", strategy=strategy, source=source,
                      note=where)
        done = [t for t in shown if t["exit_ms"] is not None]
        held = [t for t in shown if t["exit_ms"] is None]
        market = pair["event"].split(" Election")[0]
        together += [{**t, "market": market} for t in done]
        foot = (f"{where}  ·  whole backtest {data['since'][:10]} to {data['generated_at'][:10]}: {money(whole)}"
                + (f"  ·  {len(held)} held at the end (locked {money(sum(t['edge'] for t in held) * contracts)})" if held else "")
                + f"  ·  {caveat}")
        pnl_chart(f"P&L  ·  {pair['name']}", [{**t, "market": market} for t in done], contracts, start, end,
                  OUT / f"pnl_{ticker}{tag}.png", strategy=strategy, footnote=foot, numbered=True)
    if a.market is None and len(pairs) > 1 and not a.best:
        start = day_ms(a.start) if a.start else min(p["series"][0][0] for p in pairs)
        end = day_ms(a.end, end=True) if a.end else max(p["series"][-1][0] for p in pairs)
        whole = sum(t["pnl"] for p in pairs for t in p["trades"] if t["exit_ms"] is not None) * contracts
        pnl_chart(f"P&L  ·  {len(pairs)} markets together", together, contracts, start, end, OUT / f"pnl_all{tag}.png",
                  strategy=strategy, numbered=False,
                  footnote=f"{len(together)} closed trades across the markets  ·  whole backtest: {money(whole)}  ·  {caveat}")


if __name__ == "__main__":
    main()
