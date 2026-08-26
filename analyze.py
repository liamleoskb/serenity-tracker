"""
Consensus & Track Record Analyzer (v2)
--------------------------------------
Reads data/history.json and writes docs/analysis.html:

  1. CONSENSUS      — where the tracked voices currently agree
  2. DIVERGENCE     — where they disagree on the same ticker (the useful list)
  3. STANCE FLIPS   — someone reversing on a name they backed
  4. TRACK RECORD   — what the price actually did after each mention,
                      per account, with recent mentions weighted more heavily
  5. BASE RATE      — the same measurement for SPY, so you can tell whether
                      any of it beat simply owning the index

Deliberately absent: any "best strategy" ranking or buy/sell suggestion.
Over a few months of data the top-ranked rule is almost always noise, and
presenting it as a recommendation would be actively misleading.

Never crashes the workflow: any failure still writes a page explaining why.
"""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

HISTORY_PATH = "data/history.json"
OUTPUT_PATH = "docs/analysis.html"
PRICE_CACHE = "data/prices.json"

HORIZONS = [5, 20, 60]      # trading days held after a mention
HALF_LIFE_DAYS = 45         # a mention's weight halves every 45 days
MIN_AGE_DAYS = 5            # too new to have an outcome yet
MIN_N_FOR_CONFIDENCE = 20   # below this, results are greyed out
BENCHMARK = "SPY"
VALID_STANCES = ("bullish", "bearish", "neutral")


def log(msg):
    print(msg, flush=True)


# ----------------------------- shared helpers -----------------------------

def parse_when(row):
    for key in ("post_date", "recorded_at"):
        v = str(row.get(key, "")).strip()
        if not v:
            continue
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass
        if v.isdigit() and len(v) >= 10:
            try:
                return datetime.fromtimestamp(int(v[:10]), tz=timezone.utc)
            except Exception:
                pass
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(v, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def clean_ticker(t):
    t = str(t).upper().strip().lstrip("$").strip()
    return t if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", t) else ""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def safe_url(u):
    """Emit a link only for http(s). Blocks javascript: and data: schemes."""
    u = str(u or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


def recency_weight(when, now):
    """1.0 today, 0.5 at HALF_LIFE_DAYS old, 0.25 at twice that."""
    age = max((now - when).days, 0)
    return 0.5 ** (age / HALF_LIFE_DAYS)


def pct(x):
    return f"{x:+.1f}%" if x is not None else "—"


# ----------------------------- price data -----------------------------

def fetch_prices(tickers, start, end):
    """
    Daily closes per ticker: {ticker: {"YYYY-MM-DD": close}}.
    Falls back to whatever is cached if the download fails.
    """
    cache = {}
    if os.path.exists(PRICE_CACHE):
        try:
            with open(PRICE_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
            if not isinstance(cache, dict):
                cache = {}
        except Exception:
            cache = {}

    try:
        import yfinance as yf
    except ImportError:
        log("yfinance not installed — using cached prices only. "
            "Add 'pip install yfinance' to the workflow.")
        return cache

    fetched = 0
    for t in sorted(tickers):
        try:
            df = yf.Ticker(t).history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                auto_adjust=True,
            )
            if df is None or df.empty:
                log(f"  no price data for {t} (bad symbol, delisted, or "
                    f"non-US listing without a suffix)")
                continue
            cache[t] = {d.strftime("%Y-%m-%d"): float(c)
                        for d, c in zip(df.index, df["Close"])}
            fetched += 1
        except Exception as e:
            log(f"  price fetch failed for {t}: {e}")

    log(f"  prices available for {len(cache)} symbols ({fetched} refreshed)")
    try:
        os.makedirs(os.path.dirname(PRICE_CACHE), exist_ok=True)
        with open(PRICE_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        log(f"  could not save price cache: {e}")
    return cache


def forward_return(prices, ticker, when, horizon):
    """
    % change from the first trading day on/after `when`, held `horizon`
    trading days. None if we can't evaluate it yet.
    """
    series = prices.get(ticker)
    if not series:
        return None
    days = sorted(series.keys())
    start_key = when.strftime("%Y-%m-%d")
    entry_idx = None
    for i, d in enumerate(days):
        if d >= start_key:
            entry_idx = i
            break
    if entry_idx is None or entry_idx + horizon >= len(days):
        return None
    entry = series[days[entry_idx]]
    exit_ = series[days[entry_idx + horizon]]
    if not entry:
        return None
    return (exit_ - entry) / entry * 100.0


# ----------------------------- 1. consensus / divergence -----------------------------

def build_consensus(history, days=30):
    """Each author's most recent stance per ticker, within the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    latest = {}
    for r in history:
        when = parse_when(r)
        t = clean_ticker(r.get("ticker", ""))
        a = r.get("author", "")
        if not when or when < cutoff or not t or not a:
            continue
        stance = r.get("stance", "neutral")
        if stance not in VALID_STANCES:
            stance = "neutral"
        key = (t, a)
        if key not in latest or when > latest[key][0]:
            latest[key] = (when, stance)

    grouped = defaultdict(lambda: {"bullish": [], "bearish": [], "neutral": []})
    for (t, a), (_, stance) in latest.items():
        grouped[t][stance].append(a)

    rows = []
    for t, d in grouped.items():
        nb, nx = len(d["bullish"]), len(d["bearish"])
        rows.append({
            "ticker": t,
            "bulls": sorted(d["bullish"]),
            "bears": sorted(d["bearish"]),
            "neutrals": sorted(d["neutral"]),
            "consensus": nb - nx,
            "divergence": min(nb, nx),   # >0 only when both sides exist
        })
    return rows


# ----------------------------- 2. stance flips -----------------------------

def find_flips(history, days=60, limit=25):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seq = defaultdict(list)
    for r in history:
        when = parse_when(r)
        stance = r.get("stance")
        t = clean_ticker(r.get("ticker", ""))
        if not when or when < cutoff or stance not in ("bullish", "bearish") or not t:
            continue
        seq[(r.get("author", ""), t)].append(
            (when, stance, r.get("post_url", ""), r.get("post_text", "")[:160]))

    flips = []
    for (author, ticker), items in seq.items():
        items.sort(key=lambda x: x[0])
        for prev, cur in zip(items, items[1:]):
            if prev[1] != cur[1]:
                flips.append({
                    "author": author, "ticker": ticker,
                    "from": prev[1], "to": cur[1],
                    "when": cur[0], "url": cur[2], "text": cur[3],
                })
    flips.sort(key=lambda f: f["when"], reverse=True)
    return flips[:limit]


# ----------------------------- 3. track record -----------------------------

def build_track_record(history, prices):
    now = datetime.now(timezone.utc)
    per_author = defaultdict(lambda: {
        h: {"n": 0, "hits": 0, "w_n": 0.0, "w_hits": 0.0, "sum_ret": 0.0}
        for h in HORIZONS})

    for r in history:
        when = parse_when(r)
        stance = r.get("stance")
        ticker = clean_ticker(r.get("ticker", ""))
        author = r.get("author", "")
        if not when or stance not in ("bullish", "bearish") or not ticker or not author:
            continue
        if (now - when).days < MIN_AGE_DAYS:
            continue

        w = recency_weight(when, now)
        for h in HORIZONS:
            ret = forward_return(prices, ticker, when, h)
            if ret is None:
                continue
            hit = (ret > 0) if stance == "bullish" else (ret < 0)
            directional = ret if stance == "bullish" else -ret
            s = per_author[author][h]
            s["n"] += 1
            s["hits"] += 1 if hit else 0
            s["w_n"] += w
            s["w_hits"] += w if hit else 0.0
            s["sum_ret"] += directional

    return per_author


def benchmark_stats(prices):
    series = prices.get(BENCHMARK)
    out = {}
    if not series:
        return out
    days = sorted(series.keys())
    for h in HORIZONS:
        ups, total, rets = 0, 0, 0.0
        for i in range(len(days) - h):
            a, b = series[days[i]], series[days[i + h]]
            if not a:
                continue
            ret = (b - a) / a * 100
            rets += ret
            ups += 1 if ret > 0 else 0
            total += 1
        if total:
            out[h] = {"up_rate": ups / total * 100, "avg": rets / total}
    return out


# ----------------------------- rendering -----------------------------

def render(consensus, flips, track, bench, history, error=None):
    now = datetime.now(timezone.utc)
    dates = [d for d in (parse_when(r) for r in history) if d]
    span_days = (max(dates) - min(dates)).days if len(dates) > 1 else 0
    total = len(history)

    if error:
        banner = (f"<div class='warn'><strong>Analysis could not run.</strong> "
                  f"{esc(error)}</div>")
    elif total == 0:
        banner = ("<div class='warn'><strong>No data yet.</strong> Once the tracker "
                  "collects mentions, this page will fill in. Mentions also need to be "
                  f"at least {MIN_AGE_DAYS} days old before any outcome can be measured."
                  "</div>")
    elif span_days < 180:
        banner = (f"<div class='warn'><strong>Sample far too small to conclude anything.</strong> "
                  f"{total} mentions spanning {span_days} days. Hit rates below will swing "
                  f"wildly as data accumulates, and whichever account looks skilled right now "
                  f"is most likely just lucky. This page is a measuring instrument that needs "
                  f"six to twelve months before its numbers mean much.</div>")
    else:
        banner = ""

    # consensus
    agree = sorted([c for c in consensus if abs(c["consensus"]) >= 2],
                   key=lambda c: -abs(c["consensus"]))[:15]
    agree_rows = "".join(
        f"<tr><td class='tk'>{esc(c['ticker'])}</td>"
        f"<td class='{'bullish' if c['consensus'] > 0 else 'bearish'}'>"
        f"{'bullish' if c['consensus'] > 0 else 'bearish'} ({abs(c['consensus'])})</td>"
        f"<td class='who'>"
        f"{esc(', '.join('@' + a for a in (c['bulls'] if c['consensus'] > 0 else c['bears'])))}"
        f"</td></tr>"
        for c in agree) or "<tr><td colspan='3'>No ticker has 2+ voices aligned yet.</td></tr>"

    split = sorted([c for c in consensus if c["divergence"] > 0],
                   key=lambda c: (-c["divergence"], c["ticker"]))[:15]
    split_rows = "".join(
        f"<tr><td class='tk'>{esc(c['ticker'])}</td>"
        f"<td class='bullish who'>{esc(', '.join('@' + a for a in c['bulls']))}</td>"
        f"<td class='bearish who'>{esc(', '.join('@' + a for a in c['bears']))}</td></tr>"
        for c in split) or "<tr><td colspan='3'>No disagreements recorded yet.</td></tr>"

    # flips
    flip_html = "".join(
        f"<div class='card'><div class='card-top'>"
        f"<span><span class='tk'>{esc(f['ticker'])}</span>"
        f"<span class='who'>@{esc(f['author'])}</span></span>"
        f"<span class='badge {f['to']}'>{esc(f['from'])} → {esc(f['to'])}</span></div>"
        f"<p>{esc(f['text'])}</p><div class='meta'>{f['when'].strftime('%Y-%m-%d')}"
        + (f" · <a href='{esc(safe_url(f['url']))}'>original post</a>"
           if safe_url(f['url']) else "")
        + "</div></div>"
        for f in flips) or "<p class='meta'>No stance reversals recorded yet.</p>"

    # track record
    tr_rows = ""
    for author in sorted(track):
        cells = ""
        for h in HORIZONS:
            s = track[author][h]
            if s["n"] == 0:
                cells += "<td>—</td><td>—</td>"
                continue
            raw = s["hits"] / s["n"] * 100
            wtd = (s["w_hits"] / s["w_n"] * 100) if s["w_n"] else 0.0
            avg = s["sum_ret"] / s["n"]
            thin = " thin" if s["n"] < MIN_N_FOR_CONFIDENCE else ""
            cells += (f"<td class='{thin.strip()}'>{raw:.0f}% / {wtd:.0f}%"
                      f"<br><span class='meta'>n={s['n']}</span></td>"
                      f"<td class='{'bullish' if avg > 0 else 'bearish'}{thin}'>"
                      f"{pct(avg)}</td>")
        tr_rows += f"<tr><td class='who'>@{esc(author)}</td>{cells}</tr>"
    tr_rows = tr_rows or ("<tr><td colspan='7'>No mentions are old enough to "
                          "evaluate yet.</td></tr>")

    head_cells = "".join(
        f"<th>{h}d hit rate<br><span class='meta'>raw / weighted</span></th>"
        f"<th>{h}d avg return</th>" for h in HORIZONS)

    bench_rows = "".join(
        f"<tr><td>{h} days</td><td>{bench[h]['up_rate']:.0f}%</td>"
        f"<td>{pct(bench[h]['avg'])}</td></tr>"
        for h in HORIZONS if h in bench
    ) or "<tr><td colspan='3'>Benchmark data unavailable.</td></tr>"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Consensus &amp; Track Record</title><style>
:root {{ --bg:#0f1317; --panel:#181f26; --ink:#e9eef3; --dim:#8895a3;
  --bull:#3ecf8e; --bear:#ff6b6b; --line:#28313b; --warn:#f0b429; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); padding:20px;
  font:16px/1.55 "Avenir Next","Segoe UI",system-ui,sans-serif; }}
header {{ border-left:4px solid var(--warn); padding-left:14px; margin-bottom:16px; }}
h1 {{ margin:0; font-size:1.4rem; }}
h2 {{ margin-top:34px; font-size:.92rem; color:var(--dim);
  text-transform:uppercase; letter-spacing:.08em; }}
.sub, .meta {{ color:var(--dim); font-size:.8rem; }}
.warn {{ background:rgba(240,180,41,.1); border:1px solid var(--warn);
  border-radius:8px; padding:12px 14px; margin:16px 0; font-size:.87rem; }}
table {{ width:100%; border-collapse:collapse; margin-top:12px; }}
th,td {{ text-align:left; padding:9px 6px; border-bottom:1px solid var(--line);
  font-size:.88rem; vertical-align:top; }}
th {{ color:var(--dim); font-size:.7rem; text-transform:uppercase;
  letter-spacing:.05em; }}
.tk {{ font-weight:700; }}
.who {{ color:var(--dim); font-size:.82rem; }}
.bullish {{ color:var(--bull); }} .bearish {{ color:var(--bear); }}
.thin {{ opacity:.45; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:12px 14px; margin-top:10px; }}
.card-top {{ display:flex; justify-content:space-between; align-items:center; gap:10px; }}
.badge {{ font-size:.72rem; padding:2px 10px; border-radius:12px;
  border:1px solid currentColor; white-space:nowrap; }}
.card p {{ margin:6px 0; font-size:.88rem; }}
a {{ color:var(--bull); }}
nav a {{ font-size:.85rem; }}
footer {{ margin-top:38px; color:var(--dim); font-size:.76rem; line-height:1.7; }}
</style></head><body>
<header><h1>Consensus &amp; Track Record</h1>
<div class="sub">Generated {now.strftime('%Y-%m-%d %H:%M UTC')} ·
{total} mentions spanning {span_days} days</div></header>
<nav><a href="index.html">← Back to mentions dashboard</a></nav>
{banner}

<h2>Where they agree (last 30 days)</h2>
<table><thead><tr><th>Ticker</th><th>Consensus</th><th>Voices</th></tr></thead>
<tbody>{agree_rows}</tbody></table>

<h2>Where they disagree — usually the more interesting list</h2>
<table><thead><tr><th>Ticker</th><th>Bullish</th><th>Bearish</th></tr></thead>
<tbody>{split_rows}</tbody></table>

<h2>Recent stance reversals</h2>
{flip_html}

<h2>Track record by account</h2>
<p class="meta">Hit rate = how often the price moved the way the stance implied.
The weighted figure halves a mention's influence every {HALF_LIFE_DAYS} days, so
recent calls count more. Average return is what following that stance would have
produced directionally, before costs. Faded cells have fewer than
{MIN_N_FOR_CONFIDENCE} observations and should be ignored.</p>
<table><thead><tr><th>Account</th>{head_cells}</tr></thead>
<tbody>{tr_rows}</tbody></table>

<h2>Base rate: {BENCHMARK} over the same horizons</h2>
<p class="meta">The comparison that gives the table above its meaning. If an
account's hit rate isn't clearly higher than this, its calls carried no
information — the market simply drifted upward.</p>
<table><thead><tr><th>Horizon</th><th>% of periods up</th>
<th>Average return</th></tr></thead><tbody>{bench_rows}</tbody></table>

<footer>
Measurement only. This page deliberately does not rank strategies or produce trade
ideas: with a few months of data, whichever rule looks best is almost certainly
noise, and presenting it as a recommendation would mislead.<br>
Returns are hypothetical; they exclude fees, taxes, slippage and dividends, and
assume entry at a closing price you could not actually have obtained. Stance tags
are AI-generated and frequently misread sarcasm or context. Past patterns do not
predict future ones. Not investment advice.
</footer></body></html>"""


def write_page(html):
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Analysis written to {OUTPUT_PATH}")


# ----------------------------- main -----------------------------

def main():
    if not os.path.exists(HISTORY_PATH):
        log("No history file yet — writing placeholder page.")
        write_page(render([], [], {}, {}, [],
                          error="No history file yet. Run the tracker first."))
        return

    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            history = json.load(f)
        if not isinstance(history, list):
            history = []
    except Exception as e:
        log(f"Could not read history: {e}")
        write_page(render([], [], {}, {}, [], error=f"history.json unreadable: {e}"))
        return

    log(f"Loaded {len(history)} mentions.")
    if not history:
        write_page(render([], [], {}, {}, []))
        return

    dates = [d for d in (parse_when(r) for r in history) if d]
    if not dates:
        write_page(render([], [], {}, {}, history,
                          error="No usable dates in history."))
        return

    tickers = {clean_ticker(r.get("ticker", "")) for r in history}
    tickers = {t for t in tickers if t}
    tickers.add(BENCHMARK)
    log(f"Fetching prices for {len(tickers)} symbols...")

    prices = fetch_prices(tickers,
                          min(dates) - timedelta(days=10),
                          datetime.now(timezone.utc) + timedelta(days=1))

    consensus = build_consensus(history)
    flips = find_flips(history)
    track = build_track_record(history, prices)
    bench = benchmark_stats(prices)

    log(f"Consensus rows: {len(consensus)} · flips: {len(flips)} · "
        f"authors evaluated: {len(track)}")
    write_page(render(consensus, flips, track, bench, history))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        try:
            write_page(render([], [], {}, {}, [], error=f"{type(e).__name__}: {e}"))
        except Exception:
            pass
        sys.exit(0)   # never fail the workflow
