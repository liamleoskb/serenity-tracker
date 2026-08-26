"""
Market Voices Tracker — final
=============================
Daily pipeline run by GitHub Actions:
  1. ONE Apify (Scweet) run fetches recent posts from every tracked account
  2. Gemini extracts stock tickers + stance from those posts
  3. Results append to data/history.json (duplicates dropped)
  4. docs/index.html is rebuilt

Built against the actor's published spec (apify.com/altimis/scweet):
  * max_items is GLOBAL per run  -> one run covers all handles
  * free plan: $3.00/1,000 tweets + $0.006 run-start fee
  * free guardrails: 1,000 tweets/day, 10 runs/day, 60s between runs
  * unknown input keys are REJECTED -> only documented keys are sent
  * output: id, text, handle, tweet_url, created_at, nested user/tweet
  * created_at format: "Wed Dec 03 19:29:05 +0000 2025"
  * zero-result runs trigger escalating cooldowns -> never auto-retry

Safety properties:
  * exactly one Apify run per execution, no retries, no probing
  * never raises: any failure still writes a dashboard explaining why
  * history writes are atomic; a crash cannot corrupt the archive
  * all user-derived text is HTML-escaped; only http(s) links are emitted

Secrets: APIFY_TOKEN, APIFY_ACTOR_ID, GEMINI_API_KEY
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ============================ configuration ============================

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
APIFY_ACTOR_ID = os.environ.get("APIFY_ACTOR_ID", "altimis~scweet").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

TARGET_HANDLES = [h.strip().lstrip("@") for h in os.environ.get(
    "TARGET_HANDLES", "aleabitoreddit").split(",") if h.strip()]

# Documented minimum run size is 100; lower values are raised to 100 anyway.
MAX_ITEMS = max(int(os.environ.get("MAX_ITEMS", "100")), 100)

# Days of history to request. With a daily run, 2 gives one day of overlap
# so a skipped run loses nothing. Higher values re-fetch the same tweets and
# may be billed again (cross-run dedup is not documented), so keep it small.
LOOKBACK_DAYS = max(int(os.environ.get("LOOKBACK_DAYS", "2")), 1)

# "Latest" = strict reverse-chronological. "Top" returns more but unordered.
SEARCH_SORT = os.environ.get("SEARCH_SORT", "Latest").strip()
if SEARCH_SORT not in ("Top", "Latest"):
    SEARCH_SORT = "Latest"

# Retweets carry someone else's words under the tracked account's name, which
# would corrupt both stance attribution and the track record. Excluded by
# default. Documented alternatives: all, originals_only, replies_only,
# retweets_only, exclude_replies, exclude_retweets.
TWEET_TYPE = os.environ.get("TWEET_TYPE", "exclude_retweets").strip()

BATCH_SIZE = 25            # posts per Gemini request
COST_PER_1K = 3.00         # free-plan rate, used only for the log estimate
RUN_START_FEE = 0.006

HISTORY_PATH = "data/history.json"
DASHBOARD_PATH = "docs/index.html"
STATUS_PATH = "data/last_run.json"

VALID_STANCES = ("bullish", "bearish", "neutral")


def log(msg):
    print(msg, flush=True)


def http_json(url, payload=None, timeout=600):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip() else None


def esc(s):
    """HTML-escape any user-derived text."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def safe_url(u):
    """Emit a link only for http(s). Blocks javascript: and data: schemes."""
    u = str(u or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


# ============================ step 1: fetch ============================

def build_actor_input():
    """One search covering every handle. Documented keys only."""
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
             ).strftime("%Y-%m-%d")
    return {
        "source_mode": "search",
        "search_query": " OR ".join(f"from:{h}" for h in TARGET_HANDLES),
        "search_sort": SEARCH_SORT,
        "tweet_type": TWEET_TYPE,
        "since": since,
        "max_items": MAX_ITEMS,
    }


def extract_post(item):
    """Map one Scweet dataset item to our format, or None if unusable."""
    if not isinstance(item, dict):
        return None

    text = ""
    for key in ("text", "full_text", "fullText", "content", "rawContent"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    if not text:
        nested = item.get("tweet")
        if isinstance(nested, dict):
            for key in ("text", "full_text", "content"):
                v = nested.get(key)
                if isinstance(v, str) and v.strip():
                    text = v.strip()
                    break
    if not text:
        return None

    author = ""
    for key in ("handle", "username", "userName", "screen_name"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            author = v.strip().lstrip("@")
            break
    if not author:
        user = item.get("user")
        if isinstance(user, dict):
            for key in ("handle", "username", "screen_name"):
                v = user.get(key)
                if isinstance(v, str) and v.strip():
                    author = v.strip().lstrip("@")
                    break

    url = ""
    for key in ("tweet_url", "tweetUrl", "url", "link", "permalink"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            url = v.strip()
            break
    if not url:
        tid = item.get("id") or item.get("id_str") or item.get("conversation_id")
        if tid and author:
            url = f"https://x.com/{author}/status/{tid}"
        elif tid:
            url = f"https://x.com/i/status/{tid}"

    date = ""
    for key in ("created_at", "createdAt", "collected_at_utc", "date", "timestamp"):
        v = item.get(key)
        if v:
            date = str(v)
            break

    return {"text": text, "url": safe_url(url), "date": date, "author": author}


def fetch_posts():
    """ONE Apify run for all handles. Returns (posts, notes). Never raises."""
    if not APIFY_TOKEN or not APIFY_ACTOR_ID:
        log("ERROR: APIFY_TOKEN or APIFY_ACTOR_ID missing.")
        return [], ["Apify credentials missing — check GitHub Secrets."]

    actor_input = build_actor_input()
    log(f"Query    : {actor_input['search_query']}")
    log(f"Window   : since {actor_input['since']} ({LOOKBACK_DAYS}d) · "
        f"sort {actor_input['search_sort']} · {actor_input['tweet_type']}")
    log(f"Cap      : {actor_input['max_items']} tweets TOTAL across all handles")
    log("Apify use: 1 run (free plan allows 10/day)\n")

    url = (f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}"
           f"/run-sync-get-dataset-items?token={APIFY_TOKEN}")

    try:
        items = http_json(url, actor_input)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            body = ""
        log(f"Apify HTTP {e.code}: {body}")
        hint = ""
        if e.code == 400:
            hint = " (an input field was rejected — compare with the actor's Input tab)"
        elif e.code in (401, 403):
            hint = " (token invalid or the actor is not enabled on your account)"
        return [], [f"Apify returned HTTP {e.code}{hint}"]
    except Exception as e:
        log(f"Apify request failed: {e}")
        return [], [f"Apify request failed: {e}"]

    if not isinstance(items, list):
        log(f"Unexpected response: {type(items).__name__} {str(items)[:200]}")
        return [], ["Apify returned an unexpected response shape."]

    log(f"Raw items returned: {len(items)}")
    if items and isinstance(items[0], dict):
        log(f"Field names: {sorted(items[0].keys())[:25]}")

    if not items:
        log("\nZero tweets returned. Likely causes, in order:")
        log("  1. Daily run limit — the run log on apify.com says")
        log("     'Daily run limit reached (n/10)' when this is it.")
        log("  2. A cooldown from earlier zero-result runs. The actor")
        log("     lengthens these deliberately; re-running makes it worse.")
        log("  3. Nothing posted inside the date window.")
        log("  Check apify.com before running again.")
        return [], ["No tweets returned — check the Apify run log for a daily "
                    "run-limit or cooldown message before retrying."]

    posts, unattributed = [], 0
    for it in items:
        p = extract_post(it)
        if not p:
            continue
        if not p["author"]:
            p["author"] = "unknown"
            unattributed += 1
        posts.append(p)

    per_author = {}
    for p in posts:
        per_author[p["author"]] = per_author.get(p["author"], 0) + 1

    log(f"\nUsable posts: {len(posts)}")
    for a, n in sorted(per_author.items(), key=lambda kv: -kv[1]):
        log(f"  @{a}: {n}")
    if unattributed:
        log(f"  ({unattributed} posts carried no author field)")

    notes = []
    est = len(posts) / 1000 * COST_PER_1K + RUN_START_FEE
    log(f"\nEstimated cost this run: ~${est:.3f} "
        f"(free-plan rate ${COST_PER_1K:.2f}/1k + ${RUN_START_FEE} run fee)")
    notes.append(f"Fetched {len(posts)} posts · est. ~${est:.3f}")

    wanted = {h.lower() for h in TARGET_HANDLES}
    seen = {a.lower() for a in per_author}
    missing = wanted - seen
    if missing:
        notes.append("Nothing in this window from: "
                     + ", ".join("@" + m for m in sorted(missing)))

    # The cap is shared across handles: warn if one account may be crowding out
    # the rest, which would otherwise fail silently.
    if len(items) >= MAX_ITEMS and len(per_author) > 1:
        top, count = max(per_author.items(), key=lambda kv: kv[1])
        if count > MAX_ITEMS * 0.6:
            msg = (f"Cap reached and @{top} used {count}/{len(posts)} of it — "
                   f"other accounts may be under-sampled. Consider raising "
                   f"MAX_ITEMS or shortening LOOKBACK_DAYS.")
            log(f"\nWARNING: {msg}")
            notes.append(msg)
    elif len(items) >= MAX_ITEMS:
        log(f"\nNote: hit the {MAX_ITEMS}-tweet cap; older posts in the window "
            f"were not fetched.")

    return posts, notes


# ============================ step 2: classify ============================

CLASSIFY_PROMPT = """You are a strict data extractor. Below are public social media
posts from stock market commentators. For EACH post, list every stock ticker
mentioned (e.g. NVDA, TSM, 005930.KS). For each ticker, judge the author's stance
in that post: "bullish", "bearish", or "neutral".

Rules:
- Skip posts with no ticker. Never invent a ticker.
- Use the exchange-suffixed symbol for non-US listings (005930.KS for Samsung
  Electronics, 2330.TW for TSMC's Taiwan listing).
- If the author is quoting or describing someone else's view, mark "neutral".

Respond ONLY with a JSON array. No markdown fences, no commentary. Schema:
[{"post_index": 0, "ticker": "NVDA", "stance": "bullish", "note": "short reason"}]

Posts:
"""


def parse_gemini_json(raw):
    """Recover the JSON array even if wrapped in fences or prose."""
    if not raw:
        return []
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except Exception:
        pass
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(cleaned[start:end + 1])
            return data if isinstance(data, list) else []
        except Exception:
            pass
    log(f"  could not parse Gemini output: {cleaned[:200]}")
    return []


def classify_batch(batch, offset):
    numbered = "\n\n".join(
        f"[{i + offset}] {p['text'][:600]}" for i, p in enumerate(batch))
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    payload = {"contents": [{"parts": [{"text": CLASSIFY_PROMPT + numbered}]}]}

    for attempt in (1, 2):
        try:
            resp = http_json(url, payload)
            parts = resp["candidates"][0]["content"]["parts"]
            return parse_gemini_json("".join(p.get("text", "") for p in parts))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                body = ""
            log(f"  Gemini HTTP {e.code}: {body}")
            if e.code == 429 and attempt == 1:
                log("  rate limited; waiting 20s for one retry")
                time.sleep(20)
                continue
            return []
        except Exception as e:
            log(f"  Gemini call failed: {e}")
            return []
    return []


def clean_ticker(t):
    t = str(t).upper().strip().lstrip("$").strip()
    return t if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", t) else ""


def classify_posts(posts):
    if not posts:
        log("No posts to classify.")
        return [], []
    if not GEMINI_API_KEY:
        log("ERROR: GEMINI_API_KEY missing.")
        return [], ["Gemini key missing — check GitHub Secrets."]

    raw_rows = []
    for start in range(0, len(posts), BATCH_SIZE):
        batch = posts[start:start + BATCH_SIZE]
        log(f"Classifying posts {start}–{start + len(batch) - 1} "
            f"with {GEMINI_MODEL}...")
        raw_rows.extend(classify_batch(batch, start))

    results = []
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        try:
            idx = int(r.get("post_index", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(posts)):
            continue
        ticker = clean_ticker(r.get("ticker", ""))
        if not ticker:
            continue
        stance = str(r.get("stance", "neutral")).lower().strip()
        if stance not in VALID_STANCES:
            stance = "neutral"
        p = posts[idx]
        results.append({
            "ticker": ticker,
            "stance": stance,
            "note": str(r.get("note", ""))[:200],
            "author": p.get("author", ""),
            "post_url": p.get("url", ""),
            "post_date": p.get("date", ""),
            "post_text": p.get("text", "")[:280],
        })

    log(f"Extracted {len(results)} ticker mentions.")
    notes = []
    if posts and not results:
        notes.append("Posts were fetched but contained no stock tickers.")
    return results, notes


# ============================ step 3: history ============================

def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        log(f"WARNING: history.json unreadable ({e}); backed up, starting fresh.")
        try:
            os.replace(HISTORY_PATH, HISTORY_PATH + ".broken")
        except Exception:
            pass
        return []


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORY_PATH)      # atomic


def dedupe_key(row):
    return (row.get("post_url") or row.get("post_text", "")[:80],
            row.get("ticker", ""))


def merge(history, new_rows):
    seen = {dedupe_key(h) for h in history}
    added = 0
    now = datetime.now(timezone.utc).isoformat()
    for r in new_rows:
        k = dedupe_key(r)
        if k in seen:
            continue
        r["recorded_at"] = now
        history.append(r)
        seen.add(k)
        added += 1
    log(f"Added {added} new mentions ({len(new_rows) - added} already on file).")
    return history


def write_status(posts_count, added_count, notes):
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump({"when": datetime.now(timezone.utc).isoformat(),
                   "posts_fetched": posts_count,
                   "mentions_added": added_count,
                   "notes": notes}, f, ensure_ascii=False, indent=1)


# ============================ step 4: dashboard ============================

def parse_when(row):
    for key in ("post_date", "recorded_at"):
        v = str(row.get(key, "")).strip()
        if not v:
            continue
        try:
            return datetime.fromisoformat(
                v.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(v, fmt)
                return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                        ).astimezone(timezone.utc)
            except Exception:
                continue
        if v.isdigit() and len(v) >= 10:
            try:
                return datetime.fromtimestamp(int(v[:10]), tz=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def summarize(history, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    table = {}
    for r in history:
        if parse_when(r) < cutoff:
            continue
        t = table.setdefault(r.get("ticker", "?"), {
            "bullish": 0, "bearish": 0, "neutral": 0,
            "total": 0, "voices": set(), "latest": "neutral", "latest_at": None})
        stance = r.get("stance", "neutral")
        if stance not in VALID_STANCES:
            stance = "neutral"
        t[stance] += 1
        t["total"] += 1
        if r.get("author"):
            t["voices"].add(r["author"])
        when = parse_when(r)
        if t["latest_at"] is None or when > t["latest_at"]:
            t["latest_at"], t["latest"] = when, stance
    return sorted(table.items(), key=lambda kv: (-kv[1]["total"], kv[0]))


def build_dashboard(history, notes):
    periods = [("7 days", 7), ("30 days", 30), ("90 days", 90)]
    tabs, tables = [], []
    for i, (label, days) in enumerate(periods):
        rows = summarize(history, days)
        body = "".join(
            f"<tr><td class='tk'>{esc(t)}</td><td>{d['total']}</td>"
            f"<td>{len(d['voices'])}</td>"
            f"<td class='bullish'>{d['bullish']}</td>"
            f"<td class='bearish'>{d['bearish']}</td>"
            f"<td class='neutral'>{d['neutral']}</td>"
            f"<td class='{d['latest']}'>{d['latest']}</td></tr>"
            for t, d in rows
        ) or "<tr><td colspan='7'>No mentions in this period yet.</td></tr>"
        active = "active" if i == 0 else ""
        tabs.append(f"<button class='tab {active}' onclick='show({i})'>{label}</button>")
        tables.append(
            f"<table class='period {active}'><thead><tr><th>Ticker</th>"
            f"<th>Mentions</th><th>Voices</th><th>Bull</th><th>Bear</th>"
            f"<th>Neutral</th><th>Latest</th></tr></thead>"
            f"<tbody>{body}</tbody></table>")

    recent = sorted(history, key=parse_when, reverse=True)[:60]
    cards = []
    for r in recent:
        u = safe_url(r.get("post_url", ""))
        link = f" · <a href='{esc(u)}'>original post</a>" if u else ""
        cards.append(
            f"<div class='card'><div class='card-top'>"
            f"<span><span class='tk'>{esc(r.get('ticker',''))}</span>"
            f"<span class='who'>@{esc(r.get('author',''))}</span></span>"
            f"<span class='badge {r.get('stance','neutral')}'>"
            f"{esc(r.get('stance',''))}</span></div>"
            f"<p>{esc(r.get('post_text',''))}</p>"
            f"<div class='meta'>{esc(r.get('note',''))}{link}</div></div>")
    feed = "".join(cards) or (
        "<p class='meta'>Nothing recorded yet. The Actions log prints exactly "
        "why the last fetch came back empty.</p>")

    note_html = ""
    if notes:
        note_html = ("<div class='notice'><strong>Last run</strong><ul>"
                     + "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul></div>")

    watched = ", ".join("@" + esc(h) for h in TARGET_HANDLES)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Voices Tracker</title><style>
:root {{ --bg:#0f1317; --panel:#181f26; --ink:#e9eef3; --dim:#8895a3;
  --bull:#3ecf8e; --bear:#ff6b6b; --line:#28313b; --warn:#f0b429; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); padding:20px;
  font:16px/1.55 "Avenir Next","Segoe UI",system-ui,sans-serif; }}
header {{ border-left:4px solid var(--bull); padding-left:14px; margin-bottom:18px; }}
h1 {{ margin:0; font-size:1.45rem; }}
h2 {{ margin-top:34px; font-size:.95rem; color:var(--dim);
  text-transform:uppercase; letter-spacing:.08em; }}
.sub, .meta {{ color:var(--dim); font-size:.8rem; }}
.sub {{ margin-top:4px; }}
nav a {{ font-size:.85rem; }}
.notice {{ background:rgba(240,180,41,.08); border:1px solid var(--warn);
  border-radius:8px; padding:10px 14px; margin:16px 0; font-size:.85rem; }}
.notice ul {{ margin:6px 0 0 18px; padding:0; }}
.tab {{ background:none; border:1px solid var(--line); color:var(--dim);
  padding:8px 16px; border-radius:20px; margin:0 8px 8px 0; font-size:.9rem; }}
.tab.active {{ color:var(--ink); border-color:var(--bull); }}
table {{ width:100%; border-collapse:collapse; margin-top:14px; display:none; }}
table.active {{ display:table; }}
th,td {{ text-align:left; padding:9px 6px; border-bottom:1px solid var(--line);
  font-size:.9rem; }}
th {{ color:var(--dim); font-size:.72rem; text-transform:uppercase;
  letter-spacing:.06em; }}
.tk {{ font-weight:700; }}
.who {{ color:var(--dim); font-size:.8rem; margin-left:8px; }}
.bullish {{ color:var(--bull); }} .bearish {{ color:var(--bear); }}
.neutral {{ color:var(--dim); }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:13px 15px; margin-top:11px; }}
.card-top {{ display:flex; justify-content:space-between; align-items:center;
  margin-bottom:6px; gap:10px; }}
.badge {{ font-size:.72rem; padding:2px 10px; border-radius:12px;
  border:1px solid currentColor; white-space:nowrap; }}
.card p {{ margin:4px 0; font-size:.9rem; }}
a {{ color:var(--bull); }}
footer {{ margin-top:38px; color:var(--dim); font-size:.76rem; line-height:1.6; }}
</style></head><body>
<header><h1>Market Voices Tracker</h1>
<div class="sub">Watching {watched} · updated {updated} ·
{len(history)} mentions on file</div></header>
<nav><a href="analysis.html">Consensus &amp; track record →</a></nav>
{note_html}
<div style="margin-top:16px">{''.join(tabs)}</div>
{''.join(tables)}
<h2>Recent mentions</h2>
{feed}
<footer>Automated research interface for public posts. Not affiliated with any
tracked account. Classifications are AI-generated and often wrong — always read
the original post before relying on anything here. Not investment advice.</footer>
<script>
function show(n) {{
  document.querySelectorAll('.period').forEach((t,i)=>t.classList.toggle('active',i===n));
  document.querySelectorAll('.tab').forEach((b,i)=>b.classList.toggle('active',i===n));
}}
</script></body></html>"""

    os.makedirs(os.path.dirname(DASHBOARD_PATH), exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Dashboard written to {DASHBOARD_PATH}")


# ============================ main ============================

def main():
    log(f"Tracking {len(TARGET_HANDLES)} account(s): "
        f"{', '.join('@' + h for h in TARGET_HANDLES)}")
    history = load_history()
    log(f"History on file: {len(history)} mentions\n")

    posts, fetch_notes = fetch_posts()
    rows, class_notes = classify_posts(posts)
    before = len(history)
    history = merge(history, rows)
    added = len(history) - before

    save_history(history)
    notes = fetch_notes + class_notes
    write_status(len(posts), added, notes)
    build_dashboard(history, notes)
    log("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(0)     # never fail the workflow
